"""
Database and Pydantic models for the document processing pipeline.

Defines:
- SQLAlchemy ORM models (documents, extracted_records)
- Pydantic schemas for API request / response validation
- Composite indexes for efficient querying
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import relationship

from app.db.database import Base


# ─────────────────────────────────────────────
#  Enums
# ─────────────────────────────────────────────

class ProcessingStatus(str, enum.Enum):
    """Document processing lifecycle states."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ─────────────────────────────────────────────
#  SQLAlchemy ORM Models
# ─────────────────────────────────────────────

class Document(Base):
    """Represents an uploaded PDF document."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_status_created", "status", "created_at"),
        Index("ix_documents_original_filename", "original_filename"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String(512), nullable=False, index=True)
    original_filename = Column(String(512), nullable=False)
    file_size = Column(Integer, nullable=False)
    file_path = Column(String(1024), nullable=False)
    storage_key = Column(String(1024), nullable=True)  # abstracted storage ref
    content_hash = Column(String(64), nullable=True, index=True)  # SHA-256 dedup
    mime_type = Column(String(128), nullable=True, default="application/pdf")
    status = Column(
        Enum(ProcessingStatus),
        default=ProcessingStatus.PENDING,
        nullable=False,
        index=True,
    )
    error_message = Column(Text, nullable=True)
    page_count = Column(Integer, nullable=True)
    extraction_method = Column(String(20), nullable=True)  # native | ocr
    processing_time_ms = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    records = relationship(
        "ExtractedRecord",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ExtractedRecord(Base):
    """A single structured record extracted from a document."""

    __tablename__ = "extracted_records"
    __table_args__ = (
        Index("ix_records_document_idx", "document_id", "record_index"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data = Column(JSON, nullable=False, default=dict)
    record_index = Column(Integer, nullable=False, default=0)
    confidence_score = Column(Integer, nullable=True)  # 0-100
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    document = relationship("Document", back_populates="records")


# ─────────────────────────────────────────────
#  Pydantic Schemas — Request/Response
# ─────────────────────────────────────────────

class DocumentUploadResponse(BaseModel):
    """Response returned after uploading a document."""
    id: uuid.UUID
    filename: str
    status: ProcessingStatus
    content_hash: Optional[str] = None
    message: str = "Document uploaded and queued for processing."

    model_config = {"from_attributes": True}


class ExtractedRecordResponse(BaseModel):
    """A single extracted record for API responses."""
    id: uuid.UUID
    record_index: int
    data: dict[str, Any]
    confidence_score: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentResponse(BaseModel):
    """Full document details with extracted records."""
    id: uuid.UUID
    filename: str
    original_filename: str
    file_size: int
    status: ProcessingStatus
    error_message: Optional[str] = None
    page_count: Optional[int] = None
    extraction_method: Optional[str] = None
    processing_time_ms: Optional[int] = None
    records: list[ExtractedRecordResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentListItem(BaseModel):
    """Compact document representation for list endpoints."""
    id: uuid.UUID
    filename: str
    original_filename: str
    file_size: int
    status: ProcessingStatus
    page_count: Optional[int] = None
    extraction_method: Optional[str] = None
    record_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    """Paginated list of documents."""
    total: int
    documents: list[DocumentListItem]


class ExportResponse(BaseModel):
    """Response for export operations."""
    document_id: uuid.UUID
    export_format: str = "xlsx"
    download_url: str
    message: str = "Export generated successfully."


class ErrorResponse(BaseModel):
    """Standard error response."""
    detail: str
    error_code: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    version: str
    environment: str
