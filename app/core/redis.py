"""
Redis connection management.

Provides a singleton async Redis client used by the rate limiter,
task queue, and caching layer.
"""

from __future__ import annotations

from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

_redis_client: Optional[aioredis.Redis] = None


async def init_redis() -> Optional[aioredis.Redis]:
    """Create the global Redis connection (called on app startup)."""
    global _redis_client
    try:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await _redis_client.ping()
        logger.info("Redis connected", extra={"url": settings.REDIS_URL})
        return _redis_client
    except Exception as exc:
        logger.warning(f"Redis unavailable – falling back to in-memory: {exc}")
        _redis_client = None
        return None


async def close_redis() -> None:
    """Close the Redis connection (called on app shutdown)."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis connection closed")


def get_redis() -> Optional[aioredis.Redis]:
    """Return the current Redis client (may be ``None``)."""
    return _redis_client
