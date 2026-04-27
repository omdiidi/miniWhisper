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
    Records ``(route_prefix, latency_ms, monotonic_ts)`` triples.  Bounded by
    entry count (1000) AND by time-window when computing percentiles.  This
    prevents a single hung-upload outlier (e.g. 197s) from poisoning p50 for
    the next 1000 requests.  ``percentiles(route)`` filters to the last
    ``_RECENT_WINDOW_S`` seconds (default 5min); ``percentiles(route, recent_only=False)``
    returns the full deque view for backwards compatibility.
"""

from __future__ import annotations

import statistics
import threading
import time as _time
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
    """Records per-route latency observations and computes percentiles.

    Bounded by both entry count AND time window when computing percentiles —
    so a single old outlier (e.g. a 197-second hung upload) cannot poison
    p50 for the next 1000 requests.
    """

    _MAX_ENTRIES = 1000
    # Window for percentile computation. Observations older than this are
    # excluded from p50/p95/p99. A long enough window to be stable under
    # low traffic, short enough that yesterday's outliers don't haunt today.
    _RECENT_WINDOW_S = 300.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (route, latency_ms, monotonic_timestamp_seconds)
        self._entries: deque[tuple[str, float, float]] = deque(maxlen=self._MAX_ENTRIES)

    def record(self, route: str, latency_ms: float) -> None:
        with self._lock:
            self._entries.append((route, latency_ms, _time.monotonic()))

    def percentiles(self, route: str, recent_only: bool = True) -> dict[str, float | None]:
        """Return ``{"p50": …, "p95": …, "p99": …}`` for *route*.

        Parameters
        ----------
        route:
            Route prefix (e.g. ``"transcribe/dictate"``).
        recent_only:
            If ``True`` (default), include only observations from the last
            ``_RECENT_WINDOW_S`` seconds. Set to ``False`` for the full
            entry-bounded window (legacy behavior).

        Returns ``None`` for each percentile if fewer than 2 observations exist
        in the chosen window.
        """
        cutoff = _time.monotonic() - self._RECENT_WINDOW_S if recent_only else 0.0
        with self._lock:
            values = sorted(
                ms
                for r, ms, ts in self._entries
                if r == route and ts >= cutoff
            )

        if len(values) < 2:
            return {"p50": None, "p95": None, "p99": None}

        return {
            "p50": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
        }

    def all_routes(self) -> set[str]:
        with self._lock:
            return {r for r, _, _ in self._entries}


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
