"""
Application configuration management.

All settings are loaded from environment variables with sensible defaults.
Uses Pydantic Settings for validation and type coercion.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ── Application ──────────────────────────────
    APP_NAME: str = "Document Processing Pipeline"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"

    # ── API ──────────────────────────────────────
    API_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: str = "*"

    # ── Security ─────────────────────────────────
    API_KEY: str = ""  # empty = authentication disabled
    RATE_LIMIT_REQUESTS: int = 60
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # ── Database ─────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/docpipeline"
    DATABASE_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ── Redis ────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Storage ──────────────────────────────────
    STORAGE_BACKEND: str = "local"  # "local" | "s3"
    UPLOAD_DIR: str = "uploads"
    EXPORT_DIR: str = "exports"
    MAX_UPLOAD_SIZE_MB: int = 50

    # S3 (optional – used when STORAGE_BACKEND=s3)
    S3_BUCKET_NAME: str = ""
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_ENDPOINT_URL: str = ""

    # ── Processing ───────────────────────────────
    OCR_LANG: str = "eng"
    MAX_RETRIES: int = 3
    RETRY_DELAY_SECONDS: float = 1.0
    CHUNK_SIZE_PAGES: int = 10  # pages per chunk for large PDFs

    # ── Logging ──────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # "json" | "text"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
