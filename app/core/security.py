"""
API security middleware and dependencies.

Provides:
- API key authentication via X-API-Key header
- Sliding-window rate limiting backed by Redis (falls back to in-memory)
- Input sanitisation helpers
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ─────────────────────────────────────────────────
#  API-Key Authentication
# ─────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: Optional[str] = Security(_api_key_header),
) -> Optional[str]:
    """
    Validate the X-API-Key header when API_KEY is configured.

    If ``settings.API_KEY`` is empty the check is skipped (open access).
    """
    if not settings.API_KEY:
        return None  # auth disabled

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key. Provide X-API-Key header.")

    # Constant-time comparison via hash to prevent timing attacks
    expected = hashlib.sha256(settings.API_KEY.encode()).digest()
    received = hashlib.sha256(api_key.encode()).digest()
    if expected != received:
        raise HTTPException(status_code=403, detail="Invalid API key.")

    return api_key


# ─────────────────────────────────────────────────
#  Rate Limiting (in-memory fallback)
# ─────────────────────────────────────────────────

class _InMemoryRateLimiter:
    """Simple sliding-window rate limiter backed by a dict.

    Used when Redis is unavailable.  Thread-safe enough for a single
    uvicorn worker; for multi-worker deployments prefer the Redis path.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_rate_limited(self, key: str, max_requests: int, window: int) -> bool:
        now = time.time()
        timestamps = self._hits[key]
        # Prune expired entries
        self._hits[key] = [t for t in timestamps if now - t < window]
        if len(self._hits[key]) >= max_requests:
            return True
        self._hits[key].append(now)
        return False


_limiter = _InMemoryRateLimiter()


async def rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces per-IP rate limiting."""
    if settings.RATE_LIMIT_REQUESTS <= 0:
        return  # disabled

    client_ip = request.client.host if request.client else "unknown"
    key = f"rl:{client_ip}"

    # Try Redis first
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if redis:
            current = await redis.incr(key)
            if current == 1:
                await redis.expire(key, settings.RATE_LIMIT_WINDOW_SECONDS)
            if current > settings.RATE_LIMIT_REQUESTS:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please try again later.",
                    headers={"Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)},
                )
            return
    except HTTPException:
        raise
    except Exception:
        pass  # fall through to in-memory limiter

    if _limiter.is_rate_limited(
        key, settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS
    ):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)},
        )


# ─────────────────────────────────────────────────
#  Input Sanitisation
# ─────────────────────────────────────────────────

_SAFE_FILENAME_RE = re.compile(r"[^\w\s\-.]")


def sanitize_filename(filename: str) -> str:
    """Strip dangerous characters from an uploaded filename."""
    name = _SAFE_FILENAME_RE.sub("", filename)
    # Collapse multiple dots / spaces
    name = re.sub(r"\.{2,}", ".", name)
    name = name.strip(". ")
    return name or "unnamed.pdf"
