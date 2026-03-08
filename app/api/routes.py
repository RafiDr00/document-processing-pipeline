"""
API routes for the document processing pipeline.

Implements REST endpoints for document upload, retrieval, listing, and export.
Integrates with Redis queue for background processing, with an automatic
fallback to FastAPI BackgroundTasks when Redis is unavailable.
"""

import hashlib
import os
import time
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import (
    active_jobs_gauge,
    document_processing_duration_seconds,
    documents_processed_total,
)
from app.core.security import rate_limit, sanitize_filename, verify_api_key
from app.db.database import get_db
from app.models.document import (
    Document,
    DocumentListItem,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    ErrorResponse,
    ExportResponse,
    ExtractedRecord,
    ProcessingStatus,
)
from app.services.excel_exporter import ExcelExporter, ExcelExportError
from app.services.pdf_extractor import PDFExtractionError, PDFExtractor
from app.services.queue import enqueue_job
from app.services.storage import get_storage

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(
    prefix="/documents",
    tags=["Documents"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)


# ─────────────────────────────────────────────
#  Inline Background Processing (fallback when Redis is unavailable)
# ─────────────────────────────────────────────


async def _process_document_inline(document_id: str, file_path: str) -> None:
    """
    Fallback background task that runs inside the API process when the
    Redis worker queue is not available.
    """
    from app.db.database import async_session_factory

    start = time.perf_counter()
    active_jobs_gauge.inc()

    document = None
    async with async_session_factory() as session:
        try:
            result = await session.execute(
                select(Document).where(Document.id == uuid.UUID(document_id))
            )
            document = result.scalar_one_or_none()
            if not document:
                logger.error(f"Document {document_id} not found for processing")
                return

            document.status = ProcessingStatus.PROCESSING
            await session.commit()

            logger.info(f"Processing document inline: {document_id}")

            extractor = PDFExtractor()
            extraction_result = extractor.extract(file_path)

            duration_ms = int((time.perf_counter() - start) * 1000)

            document.page_count = extraction_result["page_count"]
            document.extraction_method = extraction_result["method"]
            document.processing_time_ms = duration_ms
            document.status = ProcessingStatus.COMPLETED

            for idx, record_data in enumerate(extraction_result["records"]):
                session.add(
                    ExtractedRecord(
                        document_id=document.id,
                        data=record_data,
                        record_index=idx,
                    )
                )

            await session.commit()
            documents_processed_total.inc({"status": "completed"})
            document_processing_duration_seconds.observe(duration_ms / 1000)
            logger.info(
                f"Document {document_id} processed in {duration_ms}ms — "
                f"{len(extraction_result['records'])} records"
            )

        except PDFExtractionError as e:
            logger.error(f"Extraction failed for {document_id}: {e}")
            if document:
                document.status = ProcessingStatus.FAILED
                document.error_message = str(e)
                await session.commit()
            documents_processed_total.inc({"status": "failed"})

        except Exception as e:
            logger.exception(f"Unexpected error processing {document_id}: {e}")
            if document:
                try:
                    document.status = ProcessingStatus.FAILED
                    document.error_message = f"Internal error: {str(e)}"
                    await session.commit()
                except Exception:
                    logger.exception("Failed to persist error status")
            documents_processed_total.inc({"status": "error"})

        finally:
            active_jobs_gauge.dec()


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=202,
    summary="Upload a PDF document",
    description="Upload a PDF file for asynchronous text extraction and processing. "
    "The file is streamed, hashed (SHA-256), and persisted to the configured storage backend. "
    "Processing is dispatched to the Redis worker queue, with an automatic in-process fallback.",
    responses={
        202: {
            "description": "Document accepted and queued for processing",
            "content": {
                "application/json": {
                    "example": {
                        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "filename": "invoice_q1_2025.pdf",
                        "status": "pending",
                        "content_hash": "9f0c2bcf3e63afa2c5e3ce8e1e1d3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f",
                        "message": "Document uploaded and queued for processing.",
                    }
                }
            },
        },
        400: {
            "model": ErrorResponse,
            "description": "Invalid file type or empty file",
            "content": {
                "application/json": {
                    "example": {"detail": "Only PDF files are accepted. Please upload a .pdf file."}
                }
            },
        },
        413: {
            "model": ErrorResponse,
            "description": "File exceeds size limit",
            "content": {
                "application/json": {
                    "example": {"detail": "File too large. Maximum size is 50 MB."}
                }
            },
        },
    },
)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF file to process"),
    db: AsyncSession = Depends(get_db),
) -> DocumentUploadResponse:
    """Upload a PDF and queue it for background processing."""

    # ── Validate file type ──
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted. Please upload a .pdf file.",
        )

    # ── Stream-read + hash + size check ──
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total_size = 0
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    while True:
        chunk = await file.read(1024 * 256)  # 256 KB streaming chunks
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {settings.MAX_UPLOAD_SIZE_MB} MB.",
            )
        hasher.update(chunk)
        chunks.append(chunk)

    if total_size == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded.")

    content = b"".join(chunks)
    content_hash = hasher.hexdigest()
    safe_name = sanitize_filename(file.filename)

    # ── Persist via storage backend ──
    storage = get_storage()
    file_id = uuid.uuid4()
    storage_key = storage.generate_key(safe_name, prefix="uploads")
    file_path = await storage.save(content, storage_key)

    # ── Database record ──
    document = Document(
        id=file_id,
        filename=f"{file_id.hex}_{safe_name}",
        original_filename=file.filename,
        file_size=total_size,
        file_path=file_path,
        storage_key=storage_key,
        content_hash=content_hash,
        status=ProcessingStatus.PENDING,
    )
    db.add(document)
    await db.commit()

    # ── Dispatch to queue or fallback ──
    enqueued = await enqueue_job(str(file_id), file_path)
    if not enqueued:
        background_tasks.add_task(_process_document_inline, str(file_id), file_path)

    logger.info(
        f"Document uploaded: {file.filename} ({total_size} bytes) "
        f"hash={content_hash[:12]} queue={'redis' if enqueued else 'inline'}"
    )

    return DocumentUploadResponse(
        id=file_id,
        filename=file.filename,
        status=ProcessingStatus.PENDING,
        content_hash=content_hash,
        message="Document uploaded and queued for processing.",
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get document details",
    description="Retrieve a document's metadata, processing status, and all extracted records by ID.",
    responses={
        200: {
            "description": "Document found",
            "content": {
                "application/json": {
                    "example": {
                        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "filename": "a1b2c3d4_invoice_q1_2025.pdf",
                        "original_filename": "invoice_q1_2025.pdf",
                        "file_size": 125430,
                        "status": "completed",
                        "error_message": None,
                        "page_count": 3,
                        "extraction_method": "native",
                        "processing_time_ms": 342,
                        "records": [
                            {
                                "id": "f1e2d3c4-b5a6-7890-1234-567890abcdef",
                                "record_index": 0,
                                "data": {
                                    "Name": "Acme Corporation",
                                    "ID": "INV-2025-001",
                                    "Email": "billing@acme.com",
                                    "Date": "01/15/2025",
                                    "Total": "1,250.00",
                                },
                                "confidence_score": None,
                                "created_at": "2025-01-15T10:30:00Z",
                            }
                        ],
                        "created_at": "2025-01-15T10:29:45Z",
                        "updated_at": "2025-01-15T10:30:00Z",
                    }
                }
            },
        },
        404: {
            "model": ErrorResponse,
            "content": {"application/json": {"example": {"detail": "Document not found."}}},
        },
    },
)
async def get_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Retrieve full document details including extracted records."""

    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    return DocumentResponse.model_validate(document)


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List all documents",
    description="List all processed documents with optional status filtering and cursor-based pagination.",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "total": 42,
                        "documents": [
                            {
                                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                                "filename": "a1b2c3d4_invoice_q1_2025.pdf",
                                "original_filename": "invoice_q1_2025.pdf",
                                "file_size": 125430,
                                "status": "completed",
                                "page_count": 3,
                                "extraction_method": "native",
                                "record_count": 1,
                                "created_at": "2025-01-15T10:29:45Z",
                                "updated_at": "2025-01-15T10:30:00Z",
                            }
                        ],
                    }
                }
            }
        }
    },
)
async def list_documents(
    status: ProcessingStatus | None = Query(None, description="Filter by status"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(20, ge=1, le=100, description="Max records to return"),
    db: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    """List all documents with pagination and optional status filtering."""

    # Build query
    query = select(Document).order_by(Document.created_at.desc())
    count_query = select(func.count(Document.id))

    if status:
        query = query.where(Document.status == status)
        count_query = count_query.where(Document.status == status)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    documents = result.scalars().all()

    items = []
    for doc in documents:
        items.append(
            DocumentListItem(
                id=doc.id,
                filename=doc.filename,
                original_filename=doc.original_filename,
                file_size=doc.file_size,
                status=doc.status,
                page_count=doc.page_count,
                extraction_method=doc.extraction_method,
                record_count=len(doc.records) if doc.records else 0,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
            )
        )

    return DocumentListResponse(total=total, documents=items)


@router.post(
    "/{document_id}/export",
    response_model=ExportResponse,
    summary="Export document data to Excel",
    description="Generate a formatted .xlsx file containing the document's extracted records. "
    "The file can then be downloaded via the returned URL.",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "document_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "export_format": "xlsx",
                        "download_url": "/api/v1/documents/a1b2c3d4-e5f6-7890-abcd-ef1234567890/download",
                        "message": "Export generated successfully.",
                    }
                }
            }
        },
        404: {
            "model": ErrorResponse,
            "content": {"application/json": {"example": {"detail": "Document not found."}}},
        },
        400: {
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Document is not ready for export. Current status: processing"
                    }
                }
            },
        },
    },
)
async def export_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ExportResponse:
    """Export a document's extracted records to an Excel file."""

    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    if document.status != ProcessingStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Document is not ready for export. Current status: {document.status.value}",
        )

    if not document.records:
        raise HTTPException(
            status_code=400,
            detail="No extracted records available for export.",
        )

    try:
        exporter = ExcelExporter()
        records_data = [record.data for record in document.records]
        export_filename = f"{document.original_filename.rsplit('.', 1)[0]}_export.xlsx"
        exporter.export(
            records=records_data,
            filename=export_filename,
            sheet_name="Extracted Data",
        )

        download_url = f"/api/v1/documents/{document_id}/download"

        return ExportResponse(
            document_id=document_id,
            export_format="xlsx",
            download_url=download_url,
            message="Export generated successfully.",
        )

    except ExcelExportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/{document_id}/download",
    summary="Download exported Excel file",
    description="Download the generated .xlsx export for a document. Call the export endpoint first.",
    responses={
        200: {
            "description": "Excel file download",
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
        },
        404: {
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Export file not found. Generate an export first using POST /documents/{id}/export."
                    }
                }
            },
        },
    },
)
async def download_export(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download the generated Excel export for a document."""

    result = await db.execute(select(Document).where(Document.id == document_id))
    document = result.scalar_one_or_none()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    export_filename = f"{document.original_filename.rsplit('.', 1)[0]}_export.xlsx"
    export_path = os.path.join(settings.EXPORT_DIR, export_filename)

    if not os.path.isfile(export_path):
        raise HTTPException(
            status_code=404,
            detail="Export file not found. Generate an export first using POST /documents/{id}/export.",
        )

    return FileResponse(
        path=export_path,
        filename=export_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
