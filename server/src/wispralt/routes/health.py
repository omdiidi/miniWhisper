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
    Adds header ``X-Dictation-Degraded: true`` while a file job is running
    or the file transcription model is lazy-loading (the client reads it).

GET /readyz/db  (no auth)
    ``SELECT`` on the local SQLite DB plus "schema current" (observability only).
    Also reports ``usage_write_failures`` (best-effort usage rows lost).

Auth note: readiness probes intentionally bypass bearer auth so that
Kubernetes-style probes, Cloudflare health checks, and external monitoring
do not need API credentials.  These endpoints expose only model-ready /
memory-available booleans — no user data, no audio, no model output.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from wispralt import observability
from wispralt.files import pipeline as files_pipeline

router = APIRouter()


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

    Adds ``X-Dictation-Degraded: true`` response header while a file job is
    active (Whisper competes with Parakeet for unified memory) or the file
    model is lazy-loading.
    """
    parakeet_service = request.app.state.parakeet_service

    file_runner = getattr(request.app.state, "file_runner", None)
    file_job_active: bool = file_runner.active if file_runner is not None else False
    # Cold-load also competes for unified memory + GPU; flag dictation as
    # degraded during the lazy-load window so callers can warn the user.
    file_model_loading: bool = files_pipeline.is_loading()

    if not parakeet_service.ready:
        response = JSONResponse(
            status_code=503,
            content={"status": "not_ready", "detail": "Parakeet model not loaded"},
        )
    else:
        response = JSONResponse(content={"status": "ok"})

    if file_job_active or file_model_loading:
        response.headers["X-Dictation-Degraded"] = "true"

    return response


@router.get(
    "/readyz/db",
    summary="Readiness probe for the local SQLite DB — no auth required",
)
async def readyz_db(request: Request) -> JSONResponse:
    """200 when the DB answers and its schema is current, else 503 with a reason.

    ``reason`` is ``db_error`` (any ``sqlite3.Error``, e.g. the file is
    unreadable) or ``schema_outdated`` (a migration is not applied).

    Observability only: do not wire this into a restart trigger.
    """
    failures = {"usage_write_failures": observability.usage_write_failures_total.value}
    db = getattr(request.app.state, "db", None)
    reason = "db_error" if db is None else await asyncio.to_thread(db.check)
    if reason is not None:
        return JSONResponse({"status": "degraded", "reason": reason, **failures}, status_code=503)
    return JSONResponse({"status": "ok", **failures})
