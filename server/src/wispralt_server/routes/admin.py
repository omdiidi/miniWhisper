"""
routes/admin.py — Administrative endpoints for WisprAlt server.

POST /admin/rotate-key  (bearer auth with *current* key)
    Generates a new 64-hex-char API key, persists it to .env and to a
    chmod-600 fallback file, hot-swaps it in memory, and prints it to stdout
    (captured by launchd to ~/Library/Logs/WisprAlt/server.log).
    Returns only ``{"rotated": true}``; the new key is NEVER in the response
    body (v3 delta P4#6 security requirement).

GET /metrics  (bearer auth)
    Returns structured observability data for parakeet, meeting pipeline,
    memory, and disk.  Meeting fields read from app.state placeholders that
    Phase 2 will populate; they default to safe zeros.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth, observability
from ..auth import require_admin, require_api_key
from ..config import settings
from ..meeting import pipeline as meeting_pipeline
from ..ops import env_writer
from ..ops.env_writer import find_env_path
from ..users import store as users_store

logger = logging.getLogger(__name__)

router = APIRouter()

# C10: Module-level lock prevents concurrent rotate-key requests from interleaving
# rewrite_env_var and set_current_key, which would leave the in-memory key and
# the on-disk key out of sync for a brief window.
_rotate_lock = threading.Lock()

# Fallback key file — chmod 600; deleted on next successful auth (Phase 2 wires that)
_LAST_ROTATION_KEY_PATH = (
    Path.home() / "Library" / "Application Support" / "WisprAlt" / ".last-rotation-key"
)


@router.post(
    "/admin/rotate-key",
    dependencies=[Depends(require_admin)],
    summary="Rotate the break-glass admin API key (hot-swap, no restart required)",
)
async def rotate_key(request: Request) -> JSONResponse:
    """Generate and hot-swap a new break-glass admin API key.

    The break-glass admin is the env-derived fallback (``WISPRALT_API_KEY``).
    Per-employee tokens should be rotated via ``POST /admin/users/{id}/mint``
    in the admin UI; this endpoint exists ONLY to rotate the operator's
    break-glass credential without restarting the server.

    Steps
    -----
    1. Authentication requires the **admin** role (employees get 403).
    2. Generate ``secrets.token_hex(32)`` (64 hex chars).
    3. Persist to .env via ``env_writer.rewrite_env_var`` (atomic, chmod 600).
    4. Write to ``~/Library/Application Support/WisprAlt/.last-rotation-key``
       (chmod 600) as a one-time retrieval mechanism.
    5. Print ``NEW_API_KEY=<key>`` to stdout (captured by launchd log).
    6. Update the corresponding ``wispralt.users`` row's ``token_hash`` so
       Postgres-path lookups work with the new key.
    7. Update ``app.state.break_glass_token_hash`` so the in-process
       break-glass branch matches the new key.
    8. Invalidate the OLD hash from ``token_cache``.
    9. Update legacy ``auth._current_key`` for any callers still using it.
    10. Return ``{"rotated": true}`` — the key is NEVER in the response body.
    """
    # C10: env-write + Postgres update + state mutation must be atomic so that
    # concurrent rotations cannot leave on-disk, Postgres, and in-memory state
    # out of sync.
    pool = getattr(request.app.state, "db_pool", None)
    old_bg_hash = getattr(request.app.state, "break_glass_token_hash", None)
    with _rotate_lock:
        new_key = secrets.token_hex(32)
        new_hash = users_store.hash_token(new_key)

        env_path = find_env_path()

        # 1. Persist to .env atomically (chmod 600 guaranteed by env_writer)
        try:
            env_writer.rewrite_env_var(env_path, "WISPRALT_API_KEY", new_key)
        except OSError as exc:
            logger.error("Failed to rewrite .env during key rotation: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to persist new key") from exc

        # 2. Write to fallback retrieval file (chmod 600)
        try:
            _LAST_ROTATION_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _write_secure_file(_LAST_ROTATION_KEY_PATH, new_key)
        except OSError as exc:
            logger.warning("Could not write last-rotation-key file: %s", exc)

        # 3. Emit to stdout (launchd captures → server.log)
        print(f"NEW_API_KEY={new_key}", flush=True)  # noqa: T201

        # 4. Update the wispralt.users row whose token_hash matches the OLD
        #    break-glass hash so Postgres-path lookups continue to work.
        if pool is not None and old_bg_hash is not None:
            try:
                await pool.execute(
                    "UPDATE wispralt.users SET token_hash = $1, revoked_at = NULL "
                    "WHERE token_hash = $2",
                    new_hash, old_bg_hash,
                )
            except Exception as exc:  # noqa: BLE001 — typed-recovery path: log and degrade
                logger.exception("Failed to update wispralt.users row during rotation: %s", exc)
                # Continue: the env file is already rewritten. The break-glass
                # path below will still grant admin until the operator can
                # reconcile manually.

        # 5. Update break-glass hash so auth.require_api_key matches the new key.
        request.app.state.break_glass_token_hash = new_hash

        # 6. Invalidate cached entry for the OLD key.
        if old_bg_hash is not None:
            auth.token_cache.invalidate(old_bg_hash)

        # 7. Update legacy in-memory key for any callers still using current_key().
        auth.set_current_key(new_key)

    logger.info("API key rotated successfully at %s", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    return JSONResponse(content={"rotated": True})


@router.get(
    "/metrics",
    dependencies=[Depends(require_api_key)],
    summary="Structured server observability metrics",
)
async def metrics(request: Request) -> JSONResponse:
    """Return observability snapshot.

    All ``meeting.*`` fields are populated from ``app.state`` attributes that
    Phase 2 will wire up; they default to safe zero/null values here.

    Fields
    ------
    parakeet.p50_ms / p95_ms
        Percentiles of recent Parakeet inference durations (last 100 calls).
    parakeet.queue_depth
        Number of requests currently waiting for the single-thread executor
        (always 0 or 1 in steady state with one worker).
    parakeet.last_inference_at
        ISO-8601 timestamp of the most recent inference, or null.
    meeting.active
        True if a meeting pipeline job is currently running.
    meeting.completed_24h / failed_24h
        Job counts from the last 24 hours (Phase 2 wires these from JobStore).
    meeting.current_eta_s
        Estimated seconds until the active job completes, or null.
    memory.rss_mb
        Server process RSS in MiB.
    memory.available_mb
        System available RAM in MiB.
    disk.free_gb
        Free disk space on the staging volume in GiB.
    disk.staging_count
        Number of staging WAV files currently on disk.
    """
    parakeet_service = request.app.state.parakeet_service
    mem = psutil.virtual_memory()
    proc = psutil.Process(os.getpid())
    proc_mem = proc.memory_info()

    # Disk free — always from shutil (works even if staging_dir does not exist yet)
    staging_dir: Path = settings.staging_dir
    try:
        disk_usage = shutil.disk_usage(staging_dir if staging_dir.exists() else Path.home())
        free_gb = disk_usage.free // (1024 ** 3)
    except OSError:
        free_gb = 0

    # Phase 2: read directly from MeetingRunner and JobStore where available.
    meeting_runner = getattr(request.app.state, "meeting_runner", None)
    job_store = getattr(request.app.state, "job_store", None)

    meeting_active: bool = meeting_runner.active if meeting_runner is not None else False
    active_job_id: str | None = meeting_runner.active_job_id if meeting_runner is not None else None

    completed_24h: int = job_store.count_24h("done") if job_store is not None else 0
    failed_24h: int = job_store.count_24h("failed") if job_store is not None else 0

    # ETA not yet calculated — reserved for a future delta.
    current_eta_s: int | None = None
    last_inference_at: str | None = getattr(request.app.state, "parakeet_last_inference_at", None)

    _meeting_warm, _meeting_loading = meeting_pipeline.state()

    # Staging count from filesystem
    staging_count_live: int
    try:
        staging_count_live = len(list(settings.staging_dir.glob("*.wav"))) if settings.staging_dir.exists() else 0
    except OSError:
        staging_count_live = 0

    # Observability data from module-level singletons (G4).
    all_routes = observability.latency_histogram.all_routes()
    latencies_by_route = {route: observability.latency_histogram.percentiles(route) for route in sorted(all_routes)}

    return JSONResponse(
        content={
            "parakeet": {
                "p50_ms": round(parakeet_service.p50_ms(), 1),
                "p95_ms": round(parakeet_service.p95_ms(), 1),
                "queue_depth": 0,  # Phase 2: wire from executor queue size
                "last_inference_at": last_inference_at,
            },
            "meeting": {
                "active": meeting_active,
                "active_job_id": active_job_id,
                "completed_24h": completed_24h,
                "failed_24h": failed_24h,
                "current_eta_s": current_eta_s,
                "models_warm": _meeting_warm,
                "models_loading": _meeting_loading,
                "idle_seconds": round(meeting_pipeline.idle_seconds(), 1),
                "idle_eviction_threshold_s": settings.meeting_idle_eviction_seconds,
            },
            "memory": {
                "rss_mb": proc_mem.rss // (1024 * 1024),
                "available_mb": mem.available // (1024 * 1024),
            },
            "disk": {
                "free_gb": free_gb,
                "staging_count": staging_count_live,
            },
            "requests_total": observability.request_counter.as_dict(),
            "errors_total": observability.error_counter.as_dict(),
            "latencies": latencies_by_route,
            "process_uptime_seconds": round(
                time.monotonic() - observability.process_started_at_monotonic, 1
            ),
        }
    )


# M4: _find_env_path removed — use ops.env_writer.find_env_path() instead.

# ── helpers ───────────────────────────────────────────────────────────────────


def _write_secure_file(path: Path, content: str) -> None:
    """Write *content* to *path* with mode 0600, atomically.

    Uses os.open with O_CREAT|O_WRONLY|O_TRUNC and explicit chmod so the file
    is never readable by other users even for the brief moment before chmod.
    """
    # Open with restricted permissions from the start (O_CREAT sets umask-masked
    # mode; we follow up with explicit chmod to guarantee 0600 regardless of umask)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
