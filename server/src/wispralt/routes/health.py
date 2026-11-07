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
    Probes the asyncpg pool (observability only).

Auth note: readiness probes intentionally bypass bearer auth so that
Kubernetes-style probes, Cloudflare health checks, and external monitoring
do not need API credentials.  These endpoints expose only model-ready /
memory-available booleans — no user data, no audio, no model output.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from wispralt import db
from wispralt.files import pipeline as files_pipeline

router = APIRouter()

# 500ms is the budget for an external HTTP health probe. The asyncpg
# command_timeout is 10s (db.py:67); pool acquire under load can queue
# briefly. 500ms is well above the cross-region floor for Supabase
# pooler + Cloudflare tunnel + dev machine triangulation, and well below
# the watcher's own 2s probe (which has a different SLA — it runs
# once per 10s and can afford to wait).
READYZ_DB_TIMEOUT_S = 0.5


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
    summary="Readiness probe for Postgres pool — no auth required",
)
async def readyz_db(request: Request) -> JSONResponse:
    """Probe the asyncpg pool with a sub-500ms SELECT 1.

    Returns 503 when pool is None or health_check returns False — the same
    state the db_watcher uses to trigger a rebuild.

    !!! OBSERVABILITY-ONLY !!! This endpoint is for monitoring/alerting,
    NOT a restart trigger. A flaky pool causing cascading restarts would
    amplify the outage this endpoint is meant to detect.
    DO NOT wire into Cloudflare origin health-check (use /healthz),
    DO NOT wire into launchd HTTP KeepAlive (uses process exit),
    DO NOT wire into any liveness probe that restarts on failure.
    External uptime monitors should alert-only.
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return JSONResponse(
            {"status": "degraded", "reason": "no_pool"},
            status_code=503,
        )
    ok = await db.health_check(pool, timeout_s=READYZ_DB_TIMEOUT_S)
    if not ok:
        return JSONResponse(
            {"status": "degraded", "reason": "pool_unhealthy"},
            status_code=503,
        )
    return JSONResponse({"status": "ok"})
