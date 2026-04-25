"""
observability.py — Thread-safe counters and latency histogram for /metrics.

Provides module-level singleton instances that are incremented by the request
middleware and read by ``GET /metrics``.  No external dependencies; backed by
``collections.Counter`` and ``collections.deque``.

Exported instances
------------------
request_counter : Counter
    Keyed by ``(route_prefix, status_code)``.  Incremented once per response.
error_counter : Counter
    Keyed by ``(route_prefix, status_code)`` for 4xx/5xx responses only.
latency_histogram : LatencyHistogram
    Records ``(route_prefix, latency_ms)`` tuples.  Keeps the last 1000
    observations; exposes ``p50``, ``p95``, ``p99`` per route.
"""

from __future__ import annotations

import statistics
import threading
from collections import Counter, deque


class ThreadSafeCounter:
    """Thin thread-safe wrapper around ``collections.Counter``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter: Counter[tuple[str, int]] = Counter()

    def increment(self, route: str, status: int) -> None:
        with self._lock:
            self._counter[(route, status)] += 1

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {f"{r}:{s}": v for (r, s), v in self._counter.items()}

    @property
    def total(self) -> int:
        with self._lock:
            return sum(self._counter.values())


class LatencyHistogram:
    """Records per-route latency observations and computes percentiles."""

    _MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: deque[tuple[str, float]] = deque(maxlen=self._MAX_ENTRIES)

    def record(self, route: str, latency_ms: float) -> None:
        with self._lock:
            self._entries.append((route, latency_ms))

    def percentiles(self, route: str) -> dict[str, float | None]:
        """Return ``{"p50": …, "p95": …, "p99": …}`` for *route*.

        Returns ``None`` for each percentile if fewer than 2 observations exist.
        """
        with self._lock:
            values = sorted(ms for r, ms in self._entries if r == route)

        if len(values) < 2:
            return {"p50": None, "p95": None, "p99": None}

        return {
            "p50": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
        }

    def all_routes(self) -> set[str]:
        with self._lock:
            return {r for r, _ in self._entries}


def _percentile(sorted_values: list[float], q: float) -> float:
    """Return the *q*-th quantile of a pre-sorted list (linear interpolation)."""
    n = len(sorted_values)
    idx = q * (n - 1)
    lo = int(idx)
    hi = lo + 1
    frac = idx - lo
    if hi >= n:
        return sorted_values[-1]
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


# ── Module-level singletons ────────────────────────────────────────────────────

request_counter = ThreadSafeCounter()
error_counter = ThreadSafeCounter()
latency_histogram = LatencyHistogram()
