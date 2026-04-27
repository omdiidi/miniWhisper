"""
observability.py — Thread-safe counters and latency histogram for /metrics.

Provides module-level singleton instances that are incremented by the request
middleware and read by ``GET /metrics``.  No external dependencies; backed by
``collections.Counter`` and ``collections.deque``.

Exported instances
------------------
request_counter : ThreadSafeCounter
    Keyed by ``(route_prefix, status_code)``.  Incremented once per response.
error_counter : ThreadSafeCounter
    Keyed by ``(route_prefix, status_code)`` for 4xx/5xx responses only.
latency_histogram : LatencyHistogram
    Records ``(latency_ms, monotonic_ts)`` per route in **per-route** deques
    (bounded individually) so a flood on one route cannot evict another route's
    samples.  Percentiles default to a 5-minute recent window with a low-traffic
    fallback to the full deque when the recent window has too few samples.
process_started_at_monotonic : float
    Captured at module import time; used by the `/metrics` route to expose
    ``process_uptime_seconds``.
"""

from __future__ import annotations

import statistics
import threading
import time as _time
from collections import Counter, defaultdict, deque

# Allow operators to widen the recent percentile window for low-traffic
# deployments where 5 min is too short to accumulate 2+ samples per route.
import os


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


def _env_window_s() -> float:
    raw = os.environ.get("WISPRALT_LATENCY_WINDOW_S", "")
    try:
        v = float(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return 300.0


class LatencyHistogram:
    """Per-route latency observations with time-windowed percentiles.

    Each route has its own ``deque(maxlen=_PER_ROUTE_MAX)`` so a flood on one
    route (e.g. unauth ``/readyz/*`` probes) cannot evict another route's
    samples.  Percentiles default to a recent time window
    (``WISPRALT_LATENCY_WINDOW_S`` env var, 300s default) so a single old
    outlier — e.g. a 197-second hung upload — cannot poison p50 indefinitely.

    Low-traffic fallback: if the recent window has < 2 samples, fall back to
    the full per-route deque (bounded by entry count) so a sparse-traffic
    homelab does not see permanent ``null`` percentiles.
    """

    # Cap per route, not global.  500 entries × 5 routes = 2500 max in steady
    # state — bounded memory and no cross-route eviction.
    _PER_ROUTE_MAX = 500
    _RECENT_WINDOW_S = _env_window_s()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_route: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self._PER_ROUTE_MAX)
        )

    def record(self, route: str, latency_ms: float) -> None:
        with self._lock:
            self._by_route[route].append((latency_ms, _time.monotonic()))

    def percentiles(self, route: str, recent_only: bool = True) -> dict[str, float | None]:
        """Return ``{"p50": …, "p95": …, "p99": …}`` for *route*.

        Parameters
        ----------
        route:
            Route prefix (e.g. ``"transcribe/dictate"``).
        recent_only:
            If ``True`` (default), filter to the last ``_RECENT_WINDOW_S``
            seconds; if that window has fewer than 2 observations, fall back
            to the full per-route deque so sparse-traffic deployments still
            get useful numbers.  Set to ``False`` to skip the time filter
            entirely.
        """
        with self._lock:
            entries = list(self._by_route.get(route, ()))

        if not entries:
            return {"p50": None, "p95": None, "p99": None}

        if recent_only:
            cutoff = _time.monotonic() - self._RECENT_WINDOW_S
            recent = [ms for ms, ts in entries if ts >= cutoff]
            values = sorted(recent) if len(recent) >= 2 else sorted(ms for ms, _ in entries)
        else:
            values = sorted(ms for ms, _ in entries)

        if len(values) < 2:
            return {"p50": None, "p95": None, "p99": None}

        return {
            "p50": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
        }

    def all_routes(self) -> set[str]:
        with self._lock:
            return {r for r, dq in self._by_route.items() if dq}


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
# Captured once at module import; the `/metrics` route subtracts from
# `time.monotonic()` to expose process uptime.
process_started_at_monotonic: float = _time.monotonic()
