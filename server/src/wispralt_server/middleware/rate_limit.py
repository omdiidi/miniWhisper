"""
middleware/rate_limit.py — Per-IP rolling-window rate limiter (P5#6).

No Redis; uses in-memory defaultdict[str, deque[float]] per route group.
Two independent windows:
  - /transcribe/dictate  : 60 requests per 60-second window.
  - /transcribe/meeting POST : 4 requests per 3600-second window.

The deques are never pruned for removed IPs; on a single-user Mac mini this is
acceptable (one real IP, a handful of load-balancer IPs at most).

IP extraction (Cloudflare Tunnel deployment):
  Rate limits use the real client IP, not the Cloudflare edge node IP.
  Priority:
    1. CF-Connecting-IP  — set by Cloudflare edge; cannot be spoofed by clients
       when the origin is only reachable through Cloudflare Tunnel.
    2. Leftmost entry of X-Forwarded-For (pre-CF proxy, if any).
    3. TCP remote address (request.client.host) as last resort.
  ``trust_forwarded_headers=True`` (default) enables steps 1 & 2.  Set to
  ``False`` in environments where CF-Connecting-IP/X-Forwarded-For are not
  trustworthy.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _extract_client_ip(request: Request, *, trust_forwarded: bool = True) -> str:
    """Return the best-effort real client IP for rate-limiting.

    When behind a Cloudflare Tunnel, CF-Connecting-IP is the authoritative
    source — it is injected by Cloudflare and cannot be forged by the client.
    """
    if trust_forwarded:
        # 1. CF-Connecting-IP (Cloudflare's authoritative real-IP header)
        cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip

        # 2. Leftmost entry of X-Forwarded-For
        xff = request.headers.get("X-Forwarded-For", "").strip()
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first

    # 3. TCP remote address
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rolling-window rate limiter.

    Parameters
    ----------
    dictate_per_min:
        Maximum dictation requests per IP per 60-second rolling window.
    meeting_per_hour:
        Maximum meeting POST submissions per IP per 3600-second rolling window.
    probe_per_min:
        Maximum unauthenticated probe requests (``/readyz/*``, ``/healthz``)
        per IP per 60-second rolling window.  Cloudflare's typical health-check
        cadence is one probe every 5–10s, so 120/min comfortably accommodates
        legitimate monitoring while bounding probe-flood damage.
    trust_forwarded_headers:
        When True (default), read CF-Connecting-IP / X-Forwarded-For for the
        real client IP.  The deployment model is Cloudflare Tunnel so this is
        always safe; set to False only for local development without CF.
    """

    def __init__(
        self,
        app,  # type: ignore[type-arg]  # Starlette ASGI app; no generic needed
        *,
        dictate_per_min: int = 60,
        meeting_per_hour: int = 4,
        probe_per_min: int = 120,
        trust_forwarded_headers: bool = True,
    ) -> None:
        super().__init__(app)
        self.dictate_window = 60.0
        self.meeting_window = 3600.0
        self.probe_window = 60.0
        self.dictate_max = dictate_per_min
        self.meeting_max = meeting_per_hour
        self.probe_max = probe_per_min
        self.trust_forwarded_headers = trust_forwarded_headers
        # Separate deque-per-IP registries for the route groups
        self._dictate: dict[str, deque[float]] = defaultdict(deque)
        self._meeting: dict[str, deque[float]] = defaultdict(deque)
        self._probe: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        path = request.url.path
        ip: str = _extract_client_ip(request, trust_forwarded=self.trust_forwarded_headers)
        now = time.time()

        if path.startswith("/transcribe/dictate") or path.startswith("/v1/audio/transcriptions"):
            self._prune(self._dictate[ip], now - self.dictate_window)
            if len(self._dictate[ip]) >= self.dictate_max:
                return self._429(int(self.dictate_window), is_v1=path.startswith("/v1/"))
            self._dictate[ip].append(now)

        elif path.startswith("/transcribe/meeting") and request.method == "POST":
            self._prune(self._meeting[ip], now - self.meeting_window)
            if len(self._meeting[ip]) >= self.meeting_max:
                return self._429(int(self.meeting_window))
            self._meeting[ip].append(now)

        elif path.startswith("/readyz") or path == "/healthz":
            # Unauthenticated probes — must be throttled separately so a
            # probe flood cannot starve the real-traffic deque on the single
            # FastAPI worker.
            self._prune(self._probe[ip], now - self.probe_window)
            if len(self._probe[ip]) >= self.probe_max:
                return self._429(int(self.probe_window))
            self._probe[ip].append(now)

        return await call_next(request)

    @staticmethod
    def _prune(d: deque[float], threshold: float) -> None:
        """Remove timestamps older than *threshold* from the left of *d*."""
        while d and d[0] < threshold:
            d.popleft()

    def _429(self, retry_after: int, is_v1: bool = False) -> JSONResponse:
        headers = {"Retry-After": str(retry_after)}
        if is_v1:
            return JSONResponse(
                status_code=429,
                headers=headers,
                content={
                    "error": {
                        "message": "Rate limit exceeded. Try again in a moment.",
                        "type": "rate_limit_error",
                        "param": None,
                        "code": "rate_limit_exceeded",
                    }
                },
            )
        # PRESERVE existing native shape — do NOT change to {"detail": ...}.
        # Existing /transcribe/dictate and /transcribe/meeting consumers depend on this.
        return JSONResponse(
            status_code=429,
            headers=headers,
            content={"error": "rate limit exceeded"},
        )
