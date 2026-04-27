"""
main.py — FastAPI application entry point for WisprAlt server.

Startup sequence (Phase 2 / Task 13b)
--------------------------------------
1.  Validate .env file permissions (warn loudly if not 0600/owned by us).
2.  Assert staging and output dirs are on the same filesystem (R1#15 + P4#11).
3.  Sweep old staging WAVs (> 24 h) left over from a previous run.
4.  Instantiate JobStore (SQLite WAL, P4#4) and run orphan recovery (P5#2).
    Log {"requeue": [...], "failed": [...]}.
5.  Instantiate MeetingRunner; re-enqueue surviving pending jobs.
6.  Instantiate ParakeetService and call .load() (loads weights + warmup JIT).
7.  Bootstrap meeting pipeline models in a thread (heavy; ~7 GB load).
    Sets app.state.meeting_models_ready = True when complete.
8.  Register SIGTERM handler (P5#5): mark running jobs failed, set shutting_down,
    call sys.exit(0) so launchd ExitTimeOut=15 can observe a clean exit code.
9.  Expose state on app.state for routes and health endpoints.

Shutdown
--------
Logs "clean shutdown" message.  The SIGTERM handler handles graceful job
failure before the process is killed by launchd.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from wispralt_server.config import settings, verify_env_perms
from wispralt_server.dictate.parakeet import ParakeetService
from wispralt_server.jobs.runner import MeetingRunner
from wispralt_server.jobs.store import JobStore
from wispralt_server.meeting import install_compat_shims
from wispralt_server.meeting.output import sweep_stale_tmp
from wispralt_server.meeting.pipeline import bootstrap_models
from wispralt_server.middleware.rate_limit import RateLimitMiddleware
from wispralt_server import observability
from wispralt_server.ops import staging
from wispralt_server.ops.env_writer import find_env_path
from wispralt_server.routes import admin, dictate, health
from wispralt_server.routes import meeting as meeting_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager.

    Everything before ``yield`` runs at startup; everything after at shutdown.
    """
    # ── startup ───────────────────────────────────────────────────────────────

    # 1. Validate .env permissions
    env_path = find_env_path()
    if not verify_env_perms(env_path):
        logger.warning(
            "Continuing startup despite .env permission issues — "
            "acceptable in CI/containers but NOT in production."
        )

    # 2. R1#15 + P4#11: assert staging and output dirs share one filesystem so
    #    that os.replace in output.py is truly atomic (POSIX rename(2)).
    try:
        staging.assert_same_filesystem(
            settings.staging_dir, settings.meeting_output_dir
        )
    except RuntimeError as exc:
        # Log as ERROR — server can still start, but atomicity is compromised.
        logger.error("Filesystem check failed: %s", exc)

    # 3. Job store + orphan recovery (P4#4 WAL, P5#2 policy).
    #    recover_orphans MUST run BEFORE sweep_old so that pending jobs whose
    #    WAV files still exist are observed and their paths can be excluded from
    #    the sweep.  If sweep ran first it could delete WAVs that orphan recovery
    #    would later try to re-queue (C7 fix).
    job_store = JobStore(settings.job_db_path)
    recovery = job_store.recover_orphans()
    logger.info(
        "Orphan recovery: requeue=%s failed=%s",
        recovery["requeue"],
        recovery["failed"],
    )
    app.state.job_store = job_store

    # 4. Sweep stale staging WAVs (> 24 h) left from the previous run.
    #    Exclude WAVs referenced by any non-terminal job so re-queue candidates
    #    are not accidentally removed by the sweep (C7 fix).
    active_wav_paths = {Path(j.wav_path) for j in job_store.list_active_jobs()}
    removed = staging.sweep_old(
        settings.staging_dir,
        max_age_seconds=86400,
        exclude_paths=active_wav_paths,
    )
    if removed:
        logger.info("Startup staging sweep removed %d old WAV(s).", removed)

    # 4b. Sweep stale .tmp files in meeting output dir (I8).
    tmp_removed = sweep_stale_tmp(settings.meeting_output_dir)
    if tmp_removed:
        logger.info("Startup tmp sweep removed %d stale .tmp file(s).", tmp_removed)

    # 5. Meeting runner — re-enqueue any pending jobs that survived the restart.
    meeting_runner = MeetingRunner(job_store)
    app.state.meeting_runner = meeting_runner
    # meeting_models_ready starts False; set to True after bootstrap_models().
    app.state.meeting_models_ready = False
    app.state.shutting_down = False

    # Re-enqueueing is deferred until after models load (done below via task).

    # 6. Load Parakeet (dictation model — warm + JIT pass).
    parakeet_service = ParakeetService()
    parakeet_service.load()
    app.state.parakeet_service = parakeet_service
    app.state.parakeet_last_inference_at = None

    logger.info("WisprAlt server — dictation ready.")

    # 7. Bootstrap meeting models in a thread (P4#3; ~7 GB load, CPU-heavy).
    #    Install compat shims FIRST so torch.load & huggingface_hub kwarg
    #    translation are in place before whisperx/pyannote start loading.
    install_compat_shims()
    #    Fire bootstrap as an asyncio task so lifespan does not block the event loop.
    async def _bootstrap_and_reenqueue() -> None:
        hf_token = settings.hf_token.get_secret_value()
        try:
            await asyncio.to_thread(bootstrap_models, hf_token)
            app.state.meeting_models_ready = True
            logger.info("Meeting pipeline models ready.")
            # Now that models are loaded, re-enqueue surviving pending jobs.
            await meeting_runner.reenqueue_pending()
        except Exception:  # noqa: BLE001 — we want the traceback in err.log
            # Use logger.exception so the stack trace lands in server.error.log;
            # bare logger.error("...: %s", exc) drops the traceback and defeats
            # post-incident err-log scans.
            logger.exception("Meeting model bootstrap failed")

    # I3: Store the task so it can be cancelled cleanly during shutdown.
    app.state.bootstrap_task = asyncio.create_task(_bootstrap_and_reenqueue())

    # 8. SIGTERM handler (P5#5 + P5#ExitTimeOut).
    #    I2: Use loop.add_signal_handler so the handler runs safely in the asyncio
    #    event loop rather than interrupting arbitrary C-extension code via the raw
    #    signal module.  sys.exit(0) is replaced with os._exit(0) which is safe
    #    from an asyncio callback.
    def _sigterm_handler() -> None:
        logger.info("SIGTERM received — initiating graceful shutdown.")
        app.state.shutting_down = True
        try:
            # M1: Use the JobStore public method rather than raw SQL.
            n = job_store.fail_running_jobs("server shutdown")
            if n:
                logger.info("Marked %d running job(s) as failed on SIGTERM.", n)
        except Exception as exc:  # noqa: BLE001 — best effort; don't block shutdown
            logger.warning("Could not mark running jobs failed on SIGTERM: %s", exc)
        import os as _os
        _os._exit(0)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)

    # ── yield — server is live ─────────────────────────────────────────────────
    yield

    # ── shutdown ──────────────────────────────────────────────────────────────
    # I3: Cancel the bootstrap task if it is still running (e.g. models loading
    # when a fast shutdown is triggered).
    bootstrap_task: asyncio.Task | None = getattr(app.state, "bootstrap_task", None)
    if bootstrap_task is not None and not bootstrap_task.done():
        bootstrap_task.cancel()
        await asyncio.gather(bootstrap_task, return_exceptions=True)

    logger.info("WisprAlt server clean shutdown.")


# ── application factory ───────────────────────────────────────────────────────


class _ObservabilityMiddleware(BaseHTTPMiddleware):
    """Times each request and records it in the observability singletons (G4)."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - t0) * 1_000.0

        # First two path segments as the route key to avoid per-job-id cardinality.
        # Always emit without a leading slash so /healthz and /readyz/dictation
        # share one keyspace ("healthz", "readyz/dictation"); do not fall through
        # to request.url.path (which keeps the leading slash).
        parts = [p for p in request.url.path.strip("/").split("/") if p]
        route_key = "/".join(parts[:2]) if parts else "root"

        observability.request_counter.increment(route_key, response.status_code)
        if response.status_code >= 400:
            observability.error_counter.increment(route_key, response.status_code)
        observability.latency_histogram.record(route_key, latency_ms)

        return response


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="WisprAlt Server",
        version="0.1.0",
        description=(
            "Self-hosted dictation and meeting-transcription server. "
            "Parakeet TDT 0.6B v2 (MLX) for dictation; "
            "WhisperX + Pyannote + DeepFilterNet for meeting transcription."
        ),
        lifespan=lifespan,
        # Disable OpenAPI docs in production — no route leakage via /docs.
        # Remove the next two lines during development if you want Swagger UI.
        docs_url=None,
        redoc_url=None,
    )

    # G4: Per-request observability instrumentation (must be added before rate limiter
    # so it captures the full request including 429s).
    app.add_middleware(_ObservabilityMiddleware)

    # P5#6: Per-IP rolling-window rate limiter.
    # add_middleware must be called before the first request is handled.
    app.add_middleware(
        RateLimitMiddleware,
        dictate_per_min=settings.dictate_rate_per_min,
        meeting_per_hour=settings.meeting_rate_per_hour,
        trust_forwarded_headers=settings.trust_forwarded_headers,
    )

    # Mount routers
    # /healthz and /readyz/* — no prefix; health.py defines full paths.
    app.include_router(health.router)
    # /transcribe/dictate — auth applied per-route via Depends(require_api_key).
    app.include_router(dictate.router)
    # /admin/* — auth applied per-route via Depends(require_api_key).
    app.include_router(admin.router)
    # /transcribe/meeting — Phase 2 meeting endpoints.
    app.include_router(meeting_routes.router)

    return app


app = create_app()


# M4: _locate_env removed — use ops.env_writer.find_env_path() instead.
