"""
routes/health.py — Health and readiness endpoints.

Endpoints
---------
GET /healthz
    Always returns 200 {status: "ok"}.  No auth.  Used by Cloudflare Tunnel
    to verify the tunnel is up (a 401 from a wrong/missing bearer also proves
    the tunnel is up, but /healthz avoids that confusion).

GET /readyz/dictation  (no auth)
    Returns 200 if ParakeetService.ready is True, else 503.
    Adds header ``X-Dictation-Degraded: true`` if a meeting job is currently
    running (indicated by ``app.state.meeting_active_flag``; defaults False
    until Wave 1c wires it).

GET /readyz/meeting  (no auth)
    Returns 200 if:
      - app.state.meeting_models_ready is True  AND
      - psutil.virtual_memory().available > 2 GiB
    Otherwise 503.

Auth note: readiness probes intentionally bypass bearer auth so that
Kubernetes-style probes, Cloudflare health checks, and external monitoring
do not need API credentials.  These endpoints expose only model-ready /
memory-available booleans — no user data, no audio, no model output.
"""

from __future__ import annotations

import psutil
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_2GiB = 2 * 1024 ** 3


@router.get("/healthz", summary="Liveness probe — no auth required")
async def healthz() -> JSONResponse:
    """Return 200 immediately.  No authentication required.

    Useful for Cloudflare Tunnel health checks and load-balancer probes.
    """
    return JSONResponse(content={"status": "ok"})


@router.get(
    "/readyz/dictation",
    summary="Readiness probe for dictation (Parakeet) — no auth required",
)
async def readyz_dictation(request: Request) -> JSONResponse:
    """Return 200 if Parakeet is warmed and ready, else 503.

    Adds ``X-Dictation-Degraded: true`` response header when a meeting job is
    active (heavy meeting pipeline competes for unified memory).
    The ``meeting_active_flag`` attribute on ``app.state`` is set by the
    meeting runner in Phase 2 (defaults to False here).
    """
    parakeet_service = request.app.state.parakeet_service

    meeting_runner = getattr(request.app.state, "meeting_runner", None)
    meeting_active: bool = meeting_runner.active if meeting_runner is not None else False

    if not parakeet_service.ready:
        response = JSONResponse(
            status_code=503,
            content={"status": "not_ready", "detail": "Parakeet model not loaded"},
        )
    else:
        response = JSONResponse(content={"status": "ok"})

    if meeting_active:
        response.headers["X-Dictation-Degraded"] = "true"

    return response


@router.get(
    "/readyz/meeting",
    summary="Readiness probe for meeting pipeline — no auth required",
)
async def readyz_meeting(request: Request) -> JSONResponse:
    """Return 200 if meeting models are loaded AND >= 2 GiB RAM is free.

    ``meeting_models_ready`` is set to True in ``main.py`` lifespan once the
    meeting pipeline models are loaded (Phase 2).  It defaults to False here
    so Phase 1 deployments return a clean 503 rather than crashing.
    """
    models_ready: bool = getattr(request.app.state, "meeting_models_ready", False)
    available_bytes: int = psutil.virtual_memory().available

    if not models_ready:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "detail": "Meeting pipeline models not loaded",
                "available_mb": available_bytes // (1024 * 1024),
            },
        )

    if available_bytes < _2GiB:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "detail": "Insufficient available memory",
                "available_mb": available_bytes // (1024 * 1024),
                "required_mb": _2GiB // (1024 * 1024),
            },
        )

    return JSONResponse(
        content={
            "status": "ok",
            "available_mb": available_bytes // (1024 * 1024),
        }
    )
