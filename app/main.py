"""
Document Processing Pipeline — FastAPI Application Entry Point.

Production-ready API service for extracting structured data from PDF documents.
Includes: metrics, rate limiting, API-key auth, Redis queue integration.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as documents_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.metrics import MetricsMiddleware, collect_metrics
from app.core.redis import close_redis, init_redis
from app.db.database import close_db, init_db
from app.models.document import HealthResponse
from app.services.queue import queue_length

# Initialize logging first
setup_logging()
logger = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    logger.info(
        f"Starting {settings.APP_NAME} v{settings.APP_VERSION} "
        f"[{settings.ENVIRONMENT}]"
    )
    await init_db()
    logger.info("Database initialized")
    await init_redis()
    yield
    await close_redis()
    await close_db()
    logger.info("Application shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "A production-ready API service for extracting structured data "
        "from PDF documents. Upload PDFs, extract text and fields, "
        "and export results to Excel.\n\n"
        "**Features:** async processing via Redis queue, Prometheus metrics, "
        "API-key authentication, rate limiting, pluggable storage (local / S3)."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────
#  Middleware
# ─────────────────────────────────────────────

app.add_middleware(MetricsMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────

app.include_router(documents_router, prefix=settings.API_PREFIX)


# ── System Endpoints ────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "status": "healthy",
                        "version": "2.0.0",
                        "environment": "production",
                    }
                }
            }
        }
    },
)
async def health_check() -> HealthResponse:
    """Return application health status."""
    return HealthResponse(
        status="healthy",
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )


@app.get("/metrics", tags=["System"], summary="Prometheus metrics")
async def metrics_endpoint() -> Response:
    """Expose Prometheus-compatible metrics."""
    return Response(content=collect_metrics(), media_type="text/plain; charset=utf-8")


@app.get(
    "/",
    tags=["System"],
    summary="Root",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "service": "Document Processing Pipeline",
                        "version": "2.0.0",
                        "docs": "/docs",
                        "health": "/health",
                        "metrics": "/metrics",
                        "queue_depth": 3,
                    }
                }
            }
        }
    },
)
async def root() -> dict:
    """API root — returns basic service information."""
    pending_jobs = await queue_length()
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
        "queue_depth": pending_jobs,
    }
