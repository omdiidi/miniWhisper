"""
middleware/rate_limit.py — Per-IP rolling-window rate limiter (P5#6).

No Redis; uses in-memory defaultdict[str, deque[float]] per route group.
Two independent windows:
  - /transcribe/dictate  : 60 requests per 60-second window.
  - /transcribe/meeting POST : 4 requests per 3600-second window.

The deques are never pruned for removed IPs; on a single-user Mac mini this is
acceptable (one real IP, a handful of load-balancer IPs at most).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rolling-window rate limiter.

    Parameters
    ----------
    dictate_per_min:
        Maximum dictation requests per IP per 60-second rolling window.
    meeting_per_hour:
        Maximum meeting POST submissions per IP per 3600-second rolling window.
    """

    def __init__(
        self,
        app,  # type: ignore[type-arg]  # Starlette ASGI app; no generic needed
        *,
        dictate_per_min: int = 60,
        meeting_per_hour: int = 4,
    ) -> None:
        super().__init__(app)
        self.dictate_window = 60.0
        self.meeting_window = 3600.0
        self.dictate_max = dictate_per_min
        self.meeting_max = meeting_per_hour
        # Separate deque-per-IP registries for the two route groups
        self._dictate: dict[str, deque[float]] = defaultdict(deque)
        self._meeting: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        path = request.url.path
        ip: str = request.client.host if request.client else "unknown"
        now = time.time()

        if path.startswith("/transcribe/dictate"):
            self._prune(self._dictate[ip], now - self.dictate_window)
            if len(self._dictate[ip]) >= self.dictate_max:
                return self._429(int(self.dictate_window))
            self._dictate[ip].append(now)

        elif path.startswith("/transcribe/meeting") and request.method == "POST":
            self._prune(self._meeting[ip], now - self.meeting_window)
            if len(self._meeting[ip]) >= self.meeting_max:
                return self._429(int(self.meeting_window))
            self._meeting[ip].append(now)

        return await call_next(request)

    @staticmethod
    def _prune(d: deque[float], threshold: float) -> None:
        """Remove timestamps older than *threshold* from the left of *d*."""
        while d and d[0] < threshold:
            d.popleft()

    @staticmethod
    def _429(retry_after: int) -> JSONResponse:
        return JSONResponse(
            {"error": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
