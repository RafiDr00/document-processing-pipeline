"""
File storage abstraction layer.

Provides a unified interface for storing and retrieving files, with
pluggable backends:

- **LocalStorage** — writes to disk (default for development)
- **S3Storage** — writes to any S3-compatible object store (production)

The active backend is selected via the ``STORAGE_BACKEND`` env var.
"""

from __future__ import annotations

import os
import shutil
import uuid
from abc import ABC, abstractmethod
from typing import BinaryIO

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class StorageError(Exception):
    """Raised when a storage operation fails."""


class StorageBackend(ABC):
    """Abstract base for all storage backends."""

    @abstractmethod
    async def save(self, data: bytes | BinaryIO, key: str) -> str:
        """Persist *data* under *key*. Return the canonical path / URL."""

    @abstractmethod
    async def load(self, key: str) -> bytes:
        """Return the raw bytes for *key*."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object at *key*."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists in the store."""

    @staticmethod
    def generate_key(original_filename: str, prefix: str = "") -> str:
        """Create a collision-free storage key for *original_filename*."""
        unique = uuid.uuid4().hex[:12]
        safe = original_filename.replace(" ", "_")
        return f"{prefix}/{unique}_{safe}" if prefix else f"{unique}_{safe}"


# ─────────────────────────────────────────────────
#  Local Filesystem Backend
# ─────────────────────────────────────────────────

class LocalStorage(StorageBackend):
    """Store files on the local filesystem."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = base_dir or settings.UPLOAD_DIR
        os.makedirs(self.base_dir, exist_ok=True)

    def _full_path(self, key: str) -> str:
        return os.path.join(self.base_dir, key)

    async def save(self, data: bytes | BinaryIO, key: str) -> str:
        path = self._full_path(key)
        os.makedirs(os.path.dirname(path) or self.base_dir, exist_ok=True)
        try:
            if isinstance(data, bytes):
                with open(path, "wb") as f:
                    f.write(data)
            else:
                with open(path, "wb") as f:
                    shutil.copyfileobj(data, f)
            logger.info("File saved to local storage", extra={"key": key, "path": path})
            return path
        except Exception as exc:
            raise StorageError(f"Failed to save file locally: {exc}") from exc

    async def load(self, key: str) -> bytes:
        path = self._full_path(key)
        if not os.path.isfile(path):
            raise StorageError(f"File not found: {key}")
        with open(path, "rb") as f:
            return f.read()

    async def delete(self, key: str) -> None:
        path = self._full_path(key)
        if os.path.isfile(path):
            os.remove(path)

    async def exists(self, key: str) -> bool:
        return os.path.isfile(self._full_path(key))


# ─────────────────────────────────────────────────
#  S3-Compatible Backend
# ─────────────────────────────────────────────────

class S3Storage(StorageBackend):
    """Store files in an S3-compatible object store.

    Requires ``boto3`` at runtime (only imported when this backend is active).
    """

    def __init__(self) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise StorageError("boto3 is required for S3 storage: pip install boto3") from exc

        kwargs: dict = {
            "region_name": settings.S3_REGION,
        }
        if settings.S3_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY
            kwargs["aws_secret_access_key"] = settings.S3_SECRET_KEY
        if settings.S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL

        self._client = boto3.client("s3", **kwargs)
        self._bucket = settings.S3_BUCKET_NAME

    async def save(self, data: bytes | BinaryIO, key: str) -> str:
        try:
            body = data if isinstance(data, bytes) else data.read()
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
            url = f"s3://{self._bucket}/{key}"
            logger.info("File saved to S3", extra={"key": key, "bucket": self._bucket})
            return url
        except Exception as exc:
            raise StorageError(f"S3 upload failed: {exc}") from exc

    async def load(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"S3 download failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"S3 delete failed: {exc}") from exc

    async def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────
#  Factory
# ─────────────────────────────────────────────────

def get_storage(backend: str | None = None) -> StorageBackend:
    """Return the configured storage backend instance."""
    backend = (backend or settings.STORAGE_BACKEND).lower()
    if backend == "s3":
        return S3Storage()
    return LocalStorage()
