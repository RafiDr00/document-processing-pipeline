"""
Task queue backed by Redis.

Enqueues document-processing jobs from the API and provides a
``dequeue`` helper consumed by the standalone worker process.

Falls back to immediate inline processing when Redis is unavailable
(convenient for local development without Docker).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

QUEUE_NAME = "docpipeline:jobs"


async def enqueue_job(document_id: str, file_path: str, **extra: Any) -> bool:
    """Push a processing job onto the Redis queue.

    Returns ``True`` if the job was enqueued, ``False`` if Redis is
    unavailable (caller should fall back to inline processing).
    """
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if redis is None:
            return False

        payload = json.dumps({
            "document_id": document_id,
            "file_path": file_path,
            **extra,
        })
        await redis.lpush(QUEUE_NAME, payload)
        logger.info("Job enqueued", extra={"document_id": document_id})
        return True
    except Exception as exc:
        logger.warning(f"Failed to enqueue job: {exc}")
        return False


async def dequeue_job(timeout: int = 5) -> Optional[dict[str, Any]]:
    """Block-pop the next job from the queue (used by the worker).

    Returns ``None`` when the timeout expires with no job available.
    """
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if redis is None:
            return None

        result = await redis.brpop(QUEUE_NAME, timeout=timeout)
        if result is None:
            return None
        _, raw = result
        return json.loads(raw)
    except Exception as exc:
        logger.warning(f"Failed to dequeue job: {exc}")
        return None


async def queue_length() -> int:
    """Return the number of pending jobs (for metrics)."""
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        if redis is None:
            return 0
        return await redis.llen(QUEUE_NAME)
    except Exception:
        return 0
