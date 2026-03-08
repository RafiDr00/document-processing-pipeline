"""
Prometheus metrics for the Document Processing Pipeline.

Exposes counters, histograms, and gauges for:
- HTTP request latency & counts
- Document processing duration & outcomes
- Active job gauge

The ``/metrics`` endpoint is mounted directly in ``main.py``.
"""

from __future__ import annotations

import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────
#  Metric Stores  (lightweight — no external deps)
# ─────────────────────────────────────────────────


class _Counter:
    """Thread-safe-ish counter (good enough for a single-process worker)."""

    __slots__ = ("_name", "_help", "_values")

    def __init__(self, name: str, helptext: str) -> None:
        self._name = name
        self._help = helptext
        self._values: dict[tuple, float] = {}

    def inc(self, labels: dict[str, str] | None = None, amount: float = 1) -> None:
        key = tuple(sorted((labels or {}).items()))
        self._values[key] = self._values.get(key, 0) + amount

    def collect(self) -> str:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} counter"]
        for labels, value in self._values.items():
            lbl = ",".join(f'{k}="{v}"' for k, v in labels) if labels else ""
            lbl_str = "{" + lbl + "}" if lbl else ""
            lines.append(f"{self._name}{lbl_str} {value}")
        return "\n".join(lines)


class _Histogram:
    """Minimal histogram that tracks sum, count, and buckets."""

    __slots__ = ("_name", "_help", "_sum", "_count", "_buckets", "_bucket_counts")

    DEFAULT_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name: str, helptext: str, buckets: tuple | None = None) -> None:
        self._name = name
        self._help = helptext
        self._sum = 0.0
        self._count = 0
        self._buckets = buckets or self.DEFAULT_BUCKETS
        self._bucket_counts = {b: 0 for b in self._buckets}

    def observe(self, value: float) -> None:
        self._sum += value
        self._count += 1
        for b in self._buckets:
            if value <= b:
                self._bucket_counts[b] += 1
                break  # only increment the single matching bucket

    def collect(self) -> str:
        lines = [f"# HELP {self._name} {self._help}", f"# TYPE {self._name} histogram"]
        cumulative = 0
        for b in self._buckets:
            cumulative += self._bucket_counts[b]
            lines.append(f'{self._name}_bucket{{le="{b}"}} {cumulative}')
        lines.append(f'{self._name}_bucket{{le="+Inf"}} {self._count}')
        lines.append(f"{self._name}_sum {self._sum}")
        lines.append(f"{self._name}_count {self._count}")
        return "\n".join(lines)


class _Gauge:
    __slots__ = ("_name", "_help", "_value")

    def __init__(self, name: str, helptext: str) -> None:
        self._name = name
        self._help = helptext
        self._value: float = 0

    def inc(self) -> None:
        self._value += 1

    def dec(self) -> None:
        self._value -= 1

    def set(self, v: float) -> None:
        self._value = v

    def collect(self) -> str:
        return (
            f"# HELP {self._name} {self._help}\n"
            f"# TYPE {self._name} gauge\n"
            f"{self._name} {self._value}"
        )


# ─── Global Metric Instances ─────────────────────

http_requests_total = _Counter(
    "http_requests_total",
    "Total HTTP requests processed",
)
http_request_duration_seconds = _Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
)
documents_processed_total = _Counter(
    "documents_processed_total",
    "Total documents processed by status",
)
document_processing_duration_seconds = _Histogram(
    "document_processing_duration_seconds",
    "Time spent processing a single document",
)
active_jobs_gauge = _Gauge(
    "active_processing_jobs",
    "Number of documents currently being processed",
)


def collect_metrics() -> str:
    """Render all metrics in Prometheus exposition format."""
    sections = [
        http_requests_total.collect(),
        http_request_duration_seconds.collect(),
        documents_processed_total.collect(),
        document_processing_duration_seconds.collect(),
        active_jobs_gauge.collect(),
    ]
    return "\n\n".join(sections) + "\n"


# ─── FastAPI Middleware ──────────────────────────


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count and latency for every HTTP request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        http_requests_total.inc(
            {
                "method": request.method,
                "path": request.url.path,
                "status": str(response.status_code),
            }
        )
        http_request_duration_seconds.observe(duration)

        return response
