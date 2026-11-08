"""
main.py — FastAPI application entry point for WisprAlt server.

Startup sequence (Phase 2 / Task 13b)
--------------------------------------
1.  Validate .env file permissions (warn loudly if not 0600/owned by us).
2.  Assert staging and output dirs are on the same filesystem (R1#15 + P4#11).
3.  Sweep old staging WAVs (> 24 h) left over from a previous run.
4.  JobStore on the same DB; orphan recovery BEFORE the staging sweep (a
    running job whose input survived goes back to pending, Locked Decision 8).
    Log {"requeue": [...], "failed": [...]}.
5.  Instantiate FileJobRunner; re-enqueue surviving pending jobs.
6.  Instantiate ParakeetService and call .load() (loads weights + warmup JIT).
7.  Re-enqueue pending jobs (the file model loads lazily on the first job,
    not at startup).
8.  Register SIGTERM handler (P5#5): set shutting_down and ask uvicorn to exit
    (running jobs are NOT failed; the lifespan shutdown requeues them).
9.  Expose state on app.state for routes and health endpoints.

Before any of that, ``Database(settings.db_path).migrate()`` opens the one
SQLite DB (``app.state.db``) and its ``accounts`` / ``usage`` repositories.

Shutdown
--------
``shutting_down = True`` → ``file_runner.shutdown()`` FIRST (an interrupted
job keeps its input and is requeued with its attempt refunded), then the
daemon loops, streaming sessions and insights client, then the stores are
dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from wispralt import observability
from wispralt.accounts.store import AccountStore
from wispralt.config import settings, verify_env_perms
from wispralt.database import Database
from wispralt.dictate.parakeet import ParakeetService
from wispralt.dictate.streaming_session import (
    StreamingSessionStore,
    _streaming_sweeper,
)
from wispralt.insights.client import InsightsClient
from wispralt.insights.cron import (
    _maybe_catchup,
    _seconds_until_next_fire,
    run_weekly_insights,
)
from wispralt.jobs.runner import FileJobRunner
from wispralt.jobs.store import JobStore
from wispralt.middleware import openai_errors
from wispralt.middleware.rate_limit import RateLimitMiddleware
from wispralt.ops import background, staging
from wispralt.ops.env_writer import find_env_path
from wispralt.ops.logging_setup import configure_file_logging
from wispralt.routes import (
    admin,
    admin_data,
    admin_ui,
    dictate,
    health,
    v1_models,
    v1_transcriptions,
)
from wispralt.routes import dictate_stream as dictate_stream_routes
from wispralt.routes import me as me_routes
from wispralt.routes import transcribe_file as transcribe_file_routes
from wispralt.usage.events import UsageEvent
from wispralt.usage.store import UsageStore

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

    # Size-capped app log (launchd can't rotate without sudo). Done here, not
    # at import, so importing main in tests never touches ~/Library/Logs.
    configure_file_logging(
        settings.server_log_path,
        settings.server_log_max_bytes,
        settings.server_log_backup_count,
    )

    # 0. The one SQLite DB. DATA_DIR must already exist (setup / bootstrap
    #    create it); the server never creates it, so a server started against
    #    a missing or half-restored tree fails instead of writing into it.
    if not settings.data_dir.is_dir():
        raise RuntimeError(f"DATA_DIR does not exist: {settings.data_dir}")
    database = Database(settings.db_path)
    applied = database.migrate()
    if applied:
        logger.info("Database migrations applied: %s", applied)
    app.state.db = database
    app.state.accounts = AccountStore(database)
    app.state.usage = UsageStore(database)

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
        staging.assert_same_filesystem(settings.staging_dir, settings.outputs_dir)
    except RuntimeError as exc:
        # Log as ERROR — server can still start, but atomicity is compromised.
        logger.error("Filesystem check failed: %s", exc)

    # 2b. ffmpeg/ffprobe presence — required by the /transcribe/file endpoint
    #     to decode arbitrary audio/video containers into a canonical WAV.
    #     Fail fast at startup rather than producing an opaque 500 on first
    #     request.
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffmpeg/ffprobe not found on PATH — required for /transcribe/file. "
            "Install via 'brew install ffmpeg'."
        )
    logger.info(
        "ffmpeg/ffprobe available: %s",
        subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
    )

    # 3. Job store + orphan recovery (P4#4 WAL, P5#2 policy).
    #    recover_orphans MUST run BEFORE sweep_old so that pending jobs whose
    #    WAV files still exist are observed and their paths can be excluded from
    #    the sweep.  If sweep ran first it could delete WAVs that orphan recovery
    #    would later try to re-queue (C7 fix).
    job_store = JobStore(database, settings.data_dir)
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
    #    The same sweeps re-run daily from ops.background.daily_sweep_loop.
    #    sweep_old/sweep_chunked/sweep_stale_tmp: chunked-upload dirs use a
    #    separate 1 h TTL (meta.json mtime is bumped per chunk, so active
    #    uploads are safe); stale meeting-output .tmp files are removed too.
    removed, chunked_removed, tmp_removed = background.sweep_staging(job_store)
    if removed:
        logger.info("Startup staging sweep removed %d old WAV(s).", removed)
    if chunked_removed:
        logger.info(
            "Startup chunked sweep removed %d stale chunked upload dir(s).",
            chunked_removed,
        )
    if tmp_removed:
        logger.info("Startup tmp sweep removed %d stale .tmp file(s).", tmp_removed)

    # 4c. Phase 1 transcript-storage: zero transcript text on `jobs` rows and
    #     delete `dictations` rows older than transcript_retention_days. Same
    #     sweep is re-run every 24 h by the daily timer registered below so a
    #     long-uptime server eventually reaps.
    try:
        jobs_swept, dicts_deleted = job_store.sweep_transcripts(
            days=settings.transcript_retention_days
        )
        if jobs_swept or dicts_deleted:
            logger.info(
                "Startup transcript sweep: jobs.transcript_text zeroed=%d, "
                "dictations deleted=%d",
                jobs_swept,
                dicts_deleted,
            )
    except sqlite3.Error:
        logger.exception(
            "Startup transcript sweep failed; daily timer will retry"
        )

    # 5. File-job runner — re-enqueue any pending jobs that survived the restart.
    file_runner = FileJobRunner(job_store, app.state)
    app.state.file_runner = file_runner
    app.state.shutting_down = False

    # Phase 1 transcript-storage: holds strong refs to in-flight
    # `_persist_dictation` background tasks so asyncio doesn't GC them
    # before they complete. The dictate route adds tasks here and removes
    # them via add_done_callback. Drained on shutdown below.
    app.state.pending_persists = set()

    # Re-enqueueing is deferred until after models load (done below via task).

    # 6. Load Parakeet (dictation model — warm + JIT pass).
    parakeet_service = ParakeetService()
    parakeet_service.load()
    app.state.parakeet_service = parakeet_service
    app.state.parakeet_last_inference_at = None

    logger.info("WisprAlt server — dictation ready.")

    # 6a. Streaming-dictation session store + sweeper. Additive endpoint set
    #     at /transcribe/dictate/stream/* — backed by the same Parakeet
    #     executor, gated by streaming_max_active and per-user single-session
    #     enforcement (see streaming_session.py). The sweeper aborts idle
    #     sessions past streaming_session_ttl_s.
    app.state.streaming_sessions = StreamingSessionStore(
        max_active=settings.streaming_max_active,
        max_queue_depth=settings.streaming_max_queue_depth,
        ttl_s=settings.streaming_session_ttl_s,
        finalize_timeout_s=settings.streaming_finalize_timeout_s,
    )
    streaming_sweeper_task = asyncio.create_task(_streaming_sweeper(app))
    app.state.streaming_sweeper_task = streaming_sweeper_task

    # 6b. InsightsClient — fail-soft. None when OPENROUTER_API_KEY is unset;
    #     the weekly cron is a no-op in that case.
    app.state.insights_client = None
    if settings.openrouter_api_key:
        try:
            app.state.insights_client = InsightsClient(
                api_key=settings.openrouter_api_key,
                model=settings.insights_model,
                timeout_s=settings.insights_timeout_s,
            )
            logger.info(
                "InsightsClient initialized model=%s timeout_s=%.1f",
                settings.insights_model,
                settings.insights_timeout_s,
            )
        except Exception:
            logger.exception(
                "InsightsClient init failed — weekly insights cron will be skipped"
            )
            app.state.insights_client = None
    else:
        logger.info("InsightsClient init skipped — OPENROUTER_API_KEY unset")

    # 7. Re-enqueue any pending jobs from prior runs. The first one to execute
    #    will lazy-load mlx-whisper inside the executor thread.
    try:
        await file_runner.reenqueue_pending()
    except Exception:  # noqa: BLE001 — never let re-enqueue crash startup
        logger.exception("File-job reenqueue_pending failed; continuing startup")

    # 7c. Idle-eviction background task. Every minute, ask the pipeline to
    #     unload the file model if it has been idle past the configured
    #     threshold (default 60s). Skipped entirely when threshold is 0.
    from wispralt.files import pipeline as files_pipeline

    app.state.eviction_task = None
    if settings.file_model_idle_eviction_seconds > 0:
        app.state.eviction_task = asyncio.create_task(
            background.eviction_loop(app, file_runner, files_pipeline)
        )

    # 7d. Phase 1 transcript-storage: daily sweep loop. Re-runs the same
    #     `sweep_transcripts` that ran at startup so a long-uptime server
    #     eventually reaps. Daemon-loop pattern: broad-except + log so a
    #     transient SQLite error never kills the task.
    app.state.transcript_sweep_task = asyncio.create_task(
        background.daily_sweep_loop(
            app, job_store, lambda jid: file_runner.active_job_id == jid
        )
    )

    # 7e. Phase 2: weekly LLM insights cron. Sunday 23:00 mini-local. Sleeps
    #     to next fire, runs, repeats. Robust to clock changes — recomputes
    #     next-fire each iteration. Startup-catchup runs ONCE before the
    #     sleep loop and is fail-CLOSED unless insights_catchup_enabled=True.
    #
    #     Concurrency: install an asyncio.Lock on app.state so a catchup that
    #     overlaps the scheduled fire can short-circuit rather than double-bill
    #     OpenRouter. The lock is read inside run_weekly_insights.
    app.state.weekly_insights_lock = asyncio.Lock()

    async def _weekly_insights_cron() -> None:
        # Startup catchup runs once before the sleep loop. Wrapped in
        # broad-except so a zoneinfo or SQLite hiccup doesn't kill the
        # daemon task at boot.
        try:
            await _maybe_catchup(app)
        except Exception:  # noqa: BLE001 — daemon-loop init must not crash startup
            logger.exception(
                "Startup catchup failed; continuing to normal schedule"
            )

        while not getattr(app.state, "shutting_down", False):
            try:
                sleep_s = _seconds_until_next_fire(
                    settings.insights_timezone,
                    iso_weekday=settings.insights_schedule_weekday,
                    hour=settings.insights_schedule_hour_local,
                )
            except Exception:  # noqa: BLE001 — zoneinfo failure shouldn't kill the loop
                logger.exception(
                    "Weekly insights schedule compute failed; retrying in 1h"
                )
                sleep_s = 3600.0
            logger.info(
                "Weekly insights — sleeping %.0f s until next Sunday 23:00 local",
                sleep_s,
            )
            try:
                await asyncio.sleep(sleep_s)
            except asyncio.CancelledError:
                raise
            if getattr(app.state, "shutting_down", False):
                break
            try:
                await run_weekly_insights(app)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — daemon loop
                logger.exception("Weekly insights run failed; retrying next week")

    app.state.weekly_insights_task = asyncio.create_task(_weekly_insights_cron())

    # 8. SIGTERM handler (P5#5 + P5#ExitTimeOut).
    #    I2: Use loop.add_signal_handler so the handler runs safely in the asyncio
    #    event loop rather than interrupting arbitrary C-extension code via the raw
    #    signal module.
    #
    #    The handler signals graceful shutdown via uvicorn's `should_exit` flag,
    #    NOT `os._exit(0)`, which would skip the lifespan teardown.  We instead
    #    let uvicorn run its normal shutdown
    #    path (which invokes our lifespan ``finally`` block) and trust
    #    launchd's ExitTimeout=15 to bound the wait.
    def _sigterm_handler() -> None:
        logger.info("SIGTERM received — initiating graceful shutdown.")
        app.state.shutting_down = True

        # Trigger uvicorn graceful shutdown so the lifespan teardown runs.
        # Fallback to raising
        # SIGINT in the same loop if no uvicorn server is registered (e.g.
        # tests running the app via TestClient).
        server_obj = getattr(app.state, "uvicorn_server", None)
        if server_obj is not None:
            server_obj.should_exit = True
            return
        # Last-resort: re-raise as SIGINT so asyncio's default handler triggers
        # KeyboardInterrupt → uvicorn shutdown.  Never call os._exit — that
        # bypasses the lifespan teardown.
        signal.raise_signal(signal.SIGINT)

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        # Not the main thread (e.g. TestClient's portal): uvicorn's own
        # handler still triggers the lifespan shutdown below.
        logger.warning("SIGTERM handler not installed: %s", exc)

    # Initialize per-token rate-limit state for /v1/* routes. Idempotent.
    # Imported locally to avoid a top-of-module cycle with the auth/users modules.
    from wispralt.ratelimit_per_token import init_rate_limit_state

    init_rate_limit_state(app)

    # ── yield — server is live ─────────────────────────────────────────────────
    yield

    # ── shutdown ──────────────────────────────────────────────────────────────

    # Stop the file runner FIRST, while the DB is still open: a job it
    # interrupts keeps its staged input and goes back to pending with its
    # attempt refunded (Locked Decision 8).
    app.state.shutting_down = True
    await file_runner.shutdown()
    logger.info("Shutdown: file runner stopped.")

    # Phase 1 transcript-storage: drain in-flight dictation persists FIRST so
    # the SQLite write-lock isn't being released mid-INSERT below. The 2 s
    # budget is total across all pending persists (gather waits for ALL),
    # not 2 s each; under a SIGTERM burst some tasks may be abandoned and
    # the warning log records the count.
    pending: set[asyncio.Task[None]] | None = getattr(
        app.state, "pending_persists", None
    )
    if pending:
        logger.info(
            "Awaiting %d pending dictation persist(s) on shutdown...",
            len(pending),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Dictation persist drain timed out; %d task(s) abandoned",
                len(pending),
            )

    # Cancel the daily transcript sweep AND the weekly insights cron together.
    # Both are pure daemon loops; same cancel pattern. transcript_sweep_task
    # was a pre-existing Phase 1 shutdown miss — bundle the fix with the new
    # weekly_insights_task cancel.
    for _task_attr in ("transcript_sweep_task", "weekly_insights_task"):
        _task: asyncio.Task | None = getattr(app.state, _task_attr, None)
        if _task is not None and not _task.done():
            _task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _task

    # Cancel the idle-eviction loop first so it stops emitting log noise.
    eviction_task: asyncio.Task | None = getattr(app.state, "eviction_task", None)
    if eviction_task is not None and not eviction_task.done():
        eviction_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await eviction_task

    # Streaming-dictation sweeper + active sessions. Cancel the sweeper first
    # so it stops mutating session state, then walk surviving sessions and
    # cancel their pending inference tasks under the per-session lock. Any
    # session still in "finalizing" is honored — its route handler will
    # eventually flip status itself; we only mark "active" sessions as
    # aborted on lifespan shutdown.
    streaming_sweeper: asyncio.Task | None = getattr(
        app.state, "streaming_sweeper_task", None
    )
    if streaming_sweeper is not None and not streaming_sweeper.done():
        streaming_sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await streaming_sweeper
    streaming_store: StreamingSessionStore | None = getattr(
        app.state, "streaming_sessions", None
    )
    if streaming_store is not None:
        for _session in streaming_store.snapshot_all():
            async with _session.lock:
                # Skip terminal states ("finalized", "aborted") AND mid-flight
                # "finalizing" sessions. "finalizing" sessions are still
                # being processed by their route handler's try/finally — let
                # them complete cleanly rather than regressing their status.
                if _session.status not in ("finalized", "aborted", "finalizing"):
                    observability.streaming_sessions_aborted_total.increment(
                        "lifespan_shutdown"
                    )
                    _session.status = "aborted"
                    for _task in _session.pending_tasks.values():
                        _task.cancel()

    insights_client: InsightsClient | None = getattr(
        app.state, "insights_client", None
    )
    if insights_client is not None:
        try:
            await insights_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("insights_client.aclose failed: %s", exc)

    app.state.job_store = app.state.accounts = app.state.usage = app.state.db = None
    logger.info("Shutdown: database released.")
    logger.info("WisprAlt server clean shutdown.")


# ── application factory ───────────────────────────────────────────────────────


# Track only request-creating endpoints, NOT status-poll GETs that the
# client may hammer every few seconds.  /transcribe/file POST creates a
# job; the per-job GET is the poll path — exclude it by also filtering on
# request.method.
TRACKED_ROUTES = frozenset([
    "transcribe/dictate",
    "transcribe/file",
    "v1/audio",
    "v1/models",  # OpenAI-compat model-list probes
    "v1/audio/translations",  # unsupported-endpoint stub probes (still want visibility)
])
TRACKED_METHODS = frozenset({"POST"})

# Map a tracked route key to the canonical ``kind`` recorded on usage_events.
# Keeps "v1/audio" from emitting an unhelpful "audio" kind via the default split.
_KIND_MAP = {
    "transcribe/dictate": "dictate",
    "transcribe/file": "file",
    "v1/audio": "v1_dictate",
    "v1/models": "v1_models",
    "v1/audio/translations": "v1_translations",
}


class _ObservabilityMiddleware(BaseHTTPMiddleware):
    """Times each request and records it in the observability singletons (G4).

    Also writes one ``usage_events`` row (``UsageStore.record`` in a thread,
    about 1 ms) when the request authenticated a user on a tracked route +
    method and was not a dedup replay. Best-effort: a ``sqlite3.Error`` loses
    the row, bumps ``usage_write_failures_total`` and logs the error type;
    the response is unaffected.
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

        # Record a usage row when auth resolved a user, the route is tracked,
        # the method creates work (skip GET polls), and it was not a replay.
        user = getattr(request.state, "user", None)
        usage: UsageStore | None = getattr(request.app.state, "usage", None)
        if (
            user is not None
            and usage is not None
            and route_key in TRACKED_ROUTES
            and request.method in TRACKED_METHODS
            and not getattr(request.state, "dedup_replay", False)
        ):
            try:
                bytes_in = int(request.headers.get("content-length", 0) or 0)
            except ValueError:
                bytes_in = 0
            event = UsageEvent(
                user_id=user.id,
                ts=time.time(),
                kind=_KIND_MAP.get(route_key, route_key.split("/")[-1]),
                status=response.status_code,
                duration_ms=latency_ms,
                bytes_in=bytes_in,
                request_id=request.headers.get("x-request-id")
                or getattr(request.state, "request_id", None),
            )
            try:
                await asyncio.to_thread(usage.record, event)
            except sqlite3.Error as exc:
                observability.usage_write_failures_total.increment()
                logger.error("usage write failed: %s", type(exc).__name__)

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
            "Self-hosted dictation and file-transcription server. "
            "Parakeet TDT 0.6B v2 (MLX) for dictation; "
            "mlx-whisper for uploaded-file transcription."
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
        trust_forwarded_headers=settings.trust_forwarded_headers,
    )

    # CORS — OUTERMOST middleware. MUST be the final app.add_middleware call:
    # Starlette wraps middleware in LIFO order, so last-added runs first on the
    # request path and last on the response path. This guarantees EVERY response
    # (including rate-limit 429 envelopes from RateLimitMiddleware above) carries
    # Access-Control-Allow-Origin so browser clients see real errors instead of
    # opaque CORS failures. Imported locally to keep the top-of-module import
    # block focused on hot-path dependencies.
    from wispralt.middleware.cors import install_cors

    install_cors(app)

    # Phase 2: mount vendored HTMX + Alpine for the admin UI. Local, not CDN —
    # CSP-safe + offline-resilient. WARN (not fatal) when the directory is
    # missing so a stripped deploy doesn't refuse to boot, but the admin UI
    # will 404 on those URLs until the static files land alongside the code.
    _ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin" / "static"
    if _ADMIN_STATIC_DIR.is_dir():
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(_ADMIN_STATIC_DIR)),
            name="admin_static",
        )
    else:
        logger.warning(
            "/admin/static directory missing — HTMX/Alpine will 404 in admin UI"
        )

    # Mount routers
    # /healthz and /readyz/* — no prefix; health.py defines full paths.
    app.include_router(health.router)
    # /transcribe/dictate — auth applied per-route via Depends(require_api_key).
    app.include_router(dictate.router)
    # /transcribe/dictate/stream/* — additive streaming path; same auth as
    # /transcribe/dictate.
    app.include_router(dictate_stream_routes.router)
    # /admin/* JSON endpoints (/metrics, /admin/active, server-log) — auth per-route.
    app.include_router(admin.router)
    # /admin/* Jinja2 UI: three routers under the same prefix.
    # public_router  — /admin/login (must be reachable WITHOUT auth).
    # me_router      — /admin/me (any authenticated role: self-service for employees).
    # authed_router  — everything else, admin-only.
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.me_router)
    app.include_router(admin_ui.authed_router)
    # /admin/data — Phase 2 admin Data tab (weekly insights + drill-down).
    app.include_router(admin_data.router)
    # /transcribe/file — container-agnostic submission (ffmpeg-transcoded),
    #     chunked upload, and per-job poll/download/delete.
    app.include_router(transcribe_file_routes.router)
    # /me — JSON identity self-management (any authenticated role).
    app.include_router(me_routes.router)
    # /v1/audio/transcriptions — OpenAI-compat shim.
    app.include_router(v1_transcriptions.router)
    # /v1/models — OpenAI-compat static model list. Open WebUI + enterprise
    # clients probe this before transcribing.
    app.include_router(v1_models.router)

    # Re-shape errors on /v1/* paths to OpenAI envelope. Native routes keep their
    # default {"detail": ...} shape. Must run AFTER include_router calls.
    openai_errors.install(app)

    return app


app = create_app()


# M4: _locate_env removed — use ops.env_writer.find_env_path() instead.
