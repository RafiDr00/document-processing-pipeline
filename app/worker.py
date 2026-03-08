"""
Background worker process for the document processing pipeline.

Polls the Redis job queue and processes documents independently of
the API server. Designed to run as a separate container / process.

Usage::

    python -m app.worker
"""

from __future__ import annotations

import asyncio
import signal
import time
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.metrics import (
    active_jobs_gauge,
    document_processing_duration_seconds,
    documents_processed_total,
)
from app.core.redis import close_redis, init_redis
from app.db.database import async_session_factory, close_db, init_db
from app.models.document import Document, ExtractedRecord, ProcessingStatus
from app.services.pdf_extractor import PDFExtractionError, PDFExtractor
from app.services.queue import dequeue_job

setup_logging()
logger = get_logger("worker")
settings = get_settings()

_shutdown = False


def _handle_signal(*_) -> None:  # noqa: ANN002
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received — finishing current job …")


async def process_job(payload: dict) -> None:
    """Process a single document-processing job from the queue."""
    document_id = payload["document_id"]
    file_path = payload["file_path"]
    start = time.perf_counter()

    active_jobs_gauge.inc()
    logger.info(f"Processing job for document {document_id}")

    document = None
    async with async_session_factory() as session:
        try:
            result = await session.execute(
                select(Document).where(Document.id == uuid.UUID(document_id))
            )
            document = result.scalar_one_or_none()
            if not document:
                logger.error(f"Document {document_id} not found — skipping")
                return

            document.status = ProcessingStatus.PROCESSING
            await session.commit()

            extractor = PDFExtractor()
            extraction = extractor.extract(file_path)

            duration_ms = int((time.perf_counter() - start) * 1000)

            document.page_count = extraction["page_count"]
            document.extraction_method = extraction["method"]
            document.processing_time_ms = duration_ms
            document.status = ProcessingStatus.COMPLETED

            for idx, record_data in enumerate(extraction["records"]):
                session.add(
                    ExtractedRecord(
                        document_id=document.id,
                        data=record_data,
                        record_index=idx,
                    )
                )

            await session.commit()

            duration = duration_ms / 1000
            documents_processed_total.inc({"status": "completed"})
            document_processing_duration_seconds.observe(duration)
            logger.info(
                f"Document {document_id} completed in {duration:.2f}s — "
                f"{len(extraction['records'])} records extracted"
            )

        except PDFExtractionError as exc:
            if document:
                document.status = ProcessingStatus.FAILED
                document.error_message = str(exc)
                await session.commit()
            documents_processed_total.inc({"status": "failed"})
            logger.error(f"Extraction failed for {document_id}: {exc}")

        except Exception as exc:
            if document:
                try:
                    document.status = ProcessingStatus.FAILED
                    document.error_message = f"Worker error: {exc}"
                    await session.commit()
                except Exception:
                    logger.exception("Could not persist failure status")
            documents_processed_total.inc({"status": "error"})
            logger.exception(f"Unexpected error for {document_id}: {exc}")

        finally:
            active_jobs_gauge.dec()


async def main() -> None:
    """Worker main loop — poll the queue until shutdown."""
    logger.info(f"Worker starting [{settings.ENVIRONMENT}]")

    await init_db()
    await init_redis()

    logger.info("Worker ready — waiting for jobs …")

    while not _shutdown:
        job = await dequeue_job(timeout=2)
        if job:
            await process_job(job)
        # tiny sleep to avoid busy-wait when the queue returns immediately
        await asyncio.sleep(0.05)

    await close_redis()
    await close_db()
    logger.info("Worker shut down cleanly")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    asyncio.run(main())
