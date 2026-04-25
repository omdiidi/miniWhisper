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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from wispralt_server.config import settings, verify_env_perms
from wispralt_server.dictate.parakeet import ParakeetService
from wispralt_server.jobs.runner import MeetingRunner
from wispralt_server.jobs.store import JobStore
from wispralt_server.meeting.pipeline import bootstrap_models
from wispralt_server.middleware.rate_limit import RateLimitMiddleware
from wispralt_server.ops import staging
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
    env_path = _locate_env()
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

    # 3. Sweep stale staging WAVs (> 24 h) left from the previous run.
    removed = staging.sweep_old(settings.staging_dir, max_age_seconds=86400)
    if removed:
        logger.info("Startup staging sweep removed %d old WAV(s).", removed)

    # 4. Job store + orphan recovery (P4#4 WAL, P5#2 policy).
    job_store = JobStore(settings.job_db_path)
    recovery = job_store.recover_orphans()
    logger.info(
        "Orphan recovery: requeue=%s failed=%s",
        recovery["requeue"],
        recovery["failed"],
    )
    app.state.job_store = job_store

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
    #    We fire this as an asyncio task so lifespan does not block the event loop.
    async def _bootstrap_and_reenqueue() -> None:
        hf_token = settings.hf_token.get_secret_value()
        try:
            await asyncio.to_thread(bootstrap_models, hf_token)
            app.state.meeting_models_ready = True
            logger.info("Meeting pipeline models ready.")
            # Now that models are loaded, re-enqueue surviving pending jobs.
            await meeting_runner.reenqueue_pending()
        except Exception as exc:
            logger.error("Meeting model bootstrap failed: %s", exc)

    asyncio.create_task(_bootstrap_and_reenqueue())

    # 8. SIGTERM handler (P5#5 + P5#ExitTimeOut).
    #    Sets shutting_down flag, marks any running jobs as failed, then exits
    #    with code 0 so launchd sees a clean stop and ExitTimeOut=15 takes over.
    def _sigterm_handler(signum: int, frame: object) -> None:
        logger.info("SIGTERM received — initiating graceful shutdown.")
        app.state.shutting_down = True
        try:
            # Mark all currently running jobs as failed so clients see a
            # definitive terminal status rather than a stuck "running" entry.
            with job_store._lock:
                job_store.con.execute(
                    "UPDATE jobs SET status='failed', error='server shutdown'"
                    " WHERE status='running'"
                )
        except Exception as exc:  # noqa: BLE001 — best effort; don't block shutdown
            logger.warning("Could not mark running jobs failed on SIGTERM: %s", exc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # ── yield — server is live ─────────────────────────────────────────────────
    yield

    # ── shutdown ──────────────────────────────────────────────────────────────
    logger.info("WisprAlt server clean shutdown.")


# ── application factory ───────────────────────────────────────────────────────


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

    # P5#6: Per-IP rolling-window rate limiter.
    # add_middleware must be called before the first request is handled.
    app.add_middleware(
        RateLimitMiddleware,
        dictate_per_min=60,
        meeting_per_hour=4,
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


# ── helpers ───────────────────────────────────────────────────────────────────


def _locate_env() -> Path:
    """Find the .env file; return a Path whether or not it exists."""
    candidates = [
        Path.cwd() / "server" / ".env",
        Path.cwd() / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path.cwd() / ".env"
