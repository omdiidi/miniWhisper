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
import contextlib
import logging
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from wispralt_server import db, observability
from wispralt_server.config import settings, verify_env_perms
from wispralt_server.dictate.parakeet import ParakeetService
from wispralt_server.jobs.runner import MeetingRunner
from wispralt_server.jobs.store import JobStore
from wispralt_server.meeting import install_compat_shims
from wispralt_server.meeting.output import sweep_stale_tmp
from wispralt_server.meeting.pipeline import bootstrap_models
from wispralt_server.middleware.rate_limit import RateLimitMiddleware
from wispralt_server.ops import staging
from wispralt_server.ops.env_writer import find_env_path
from wispralt_server.routes import admin, admin_ui, dictate, health
from wispralt_server.routes import meeting as meeting_routes
from wispralt_server.usage import writer as usage_writer
from wispralt_server.usage.events import UsageEvent
from wispralt_server.users import store as users_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── lifespan ──────────────────────────────────────────────────────────────────


async def _seed_admin_if_empty(pool: asyncpg.Pool) -> None:
    """Idempotently insert a row for the env-var break-glass token.

    Bails when ``wispralt.users`` already has any row.  ``ON CONFLICT
    (token_hash) DO NOTHING`` makes a restart with the same env-var token
    (after a manual revoke) a no-op rather than a crash.
    """
    n = await pool.fetchval("SELECT COUNT(*) FROM wispralt.users")
    if n and n > 0:
        return
    bearer = settings.wispralt_api_key.get_secret_value()
    th = users_store.hash_token(bearer)
    await pool.execute(
        "INSERT INTO wispralt.users (label, token_hash, role, notes) "
        "VALUES ($1, $2, 'admin', $3) "
        "ON CONFLICT (token_hash) DO NOTHING",
        "break-glass-admin (seeded from env)",
        th,
        "Auto-seeded on first startup. Rotate via /admin/users to a real label.",
    )
    logger.info("Seeded first admin user from WISPRALT_API_KEY env var.")


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

    # 9. Auth: break-glass admin hash + Postgres pool + usage drainer.
    #    Order matters:
    #      a. break_glass_token_hash is pure compute — set it FIRST so even
    #         if the pool init below explodes, the operator can still hit
    #         /admin/* via the env-var token.
    #      b. Pre-set db_pool / usage_drainer to None so the shutdown
    #         branch can safely test them with getattr().
    #      c. Try to spin up the pool, seed the admin row, and start the
    #         drainer.  Catch only typed Postgres / OS errors.
    app.state.break_glass_token_hash = users_store.hash_token(
        settings.wispralt_api_key.get_secret_value()
    )
    app.state.db_pool = None
    app.state.usage_drainer = None

    try:
        pool = await db.get_pool()
        app.state.db_pool = pool
        await _seed_admin_if_empty(pool)
        app.state.usage_drainer = asyncio.create_task(
            usage_writer.drain_loop(observability.usage_queue, pool)
        )
    except (asyncpg.PostgresError, OSError, db.PostgresUnavailable):
        logger.exception(
            "Postgres unavailable at startup; only break-glass admin will work"
        )

    # ── yield — server is live ─────────────────────────────────────────────────
    yield

    # ── shutdown ──────────────────────────────────────────────────────────────
    # I3: Cancel the bootstrap task if it is still running (e.g. models loading
    # when a fast shutdown is triggered).
    bootstrap_task: asyncio.Task | None = getattr(app.state, "bootstrap_task", None)
    if bootstrap_task is not None and not bootstrap_task.done():
        bootstrap_task.cancel()
        await asyncio.gather(bootstrap_task, return_exceptions=True)

    # Cancel the usage-event drainer so its CancelledError handler can
    # do the final flush of any pending events.  getattr is defensive —
    # the lifespan pre-sets the attr, so we only fall through if the
    # startup branch was bypassed entirely.
    drainer: asyncio.Task | None = getattr(app.state, "usage_drainer", None)
    if drainer is not None:
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer
    await db.close_pool()

    logger.info("WisprAlt server clean shutdown.")


# ── application factory ───────────────────────────────────────────────────────


# Track only request-creating endpoints, NOT status-poll GETs that the
# client may hammer every few seconds.  /transcribe/meeting POST creates a
# job; /transcribe/meeting/{id} GET is the poll path — exclude it by also
# filtering on request.method.
TRACKED_ROUTES = frozenset(["transcribe/dictate", "transcribe/meeting"])
TRACKED_METHODS = frozenset({"POST"})


class _ObservabilityMiddleware(BaseHTTPMiddleware):
    """Times each request and records it in the observability singletons (G4).

    Also enqueues a :class:`UsageEvent` after the latency record when the
    request resolved to a real (non-break-glass) user on a tracked route +
    method.  Enqueue is fire-and-forget; the background drainer in
    ``usage.writer`` flushes batches into Postgres off the hot path.
    """

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

        # Enqueue a usage event when:
        #   - auth resolved to a real DB user (skip None and break-glass id<0),
        #   - the route is one we track, and
        #   - the method creates work (skip GET status polls).
        user = getattr(request.state, "user", None)
        if (
            user is not None
            and user.id >= 0
            and route_key in TRACKED_ROUTES
            and request.method in TRACKED_METHODS
        ):
            try:
                bytes_in = int(request.headers.get("content-length", 0) or 0)
            except ValueError:
                bytes_in = 0
            observability.usage_queue.offer(
                UsageEvent(
                    user_id=user.id,
                    ts=time.time(),
                    kind=route_key.split("/")[-1],
                    status=response.status_code,
                    duration_ms=latency_ms,
                    bytes_in=bytes_in,
                    request_id=request.headers.get("x-request-id")
                    or getattr(request.state, "request_id", None),
                )
            )

        return response


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a short request-id to every request for log correlation.

    Uses the inbound ``X-Request-Id`` header when present, else generates
    a 12-hex-char UUID slice.  Echoes it back on the response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[type-arg]
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
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
    #
    # Middleware add-order gotcha: Starlette adds OUTERMOST LAST.  We want
    # request_id assigned BEFORE observability runs so the usage-event
    # enqueue can read request.state.request_id.  Therefore add
    # _ObservabilityMiddleware first (inner), then _RequestIdMiddleware
    # (outer / runs first).
    app.add_middleware(_ObservabilityMiddleware)
    app.add_middleware(_RequestIdMiddleware)

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
    # /admin/* legacy JSON endpoints (rotate-key, /metrics) — auth per-route.
    app.include_router(admin.router)
    # /admin/* Jinja2 UI: two routers — public_router for /admin/login (must
    # be reachable WITHOUT auth), authed_router for everything else.
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.authed_router)
    # /transcribe/meeting — Phase 2 meeting endpoints.
    app.include_router(meeting_routes.router)

    return app


app = create_app()


# M4: _locate_env removed — use ops.env_writer.find_env_path() instead.
