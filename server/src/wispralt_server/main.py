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
7.  Install compat shims; re-enqueue pending jobs (meeting models load lazily
    on first job, not at startup).
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
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from wispralt_server import db, observability
from wispralt_server.config import settings, verify_env_perms
from wispralt_server.dictate.parakeet import ParakeetService
from wispralt_server.dictate.streaming_session import (
    StreamingSessionStore,
    _streaming_sweeper,
)
from wispralt_server.insights.client import InsightsClient
from wispralt_server.insights.cron import (
    _maybe_catchup,
    _seconds_until_next_fire,
    run_weekly_insights,
)
from wispralt_server.jobs.runner import MeetingRunner
from wispralt_server.jobs.store import JobStore
from wispralt_server.meeting import install_compat_shims
from wispralt_server.meeting.output import sweep_stale_tmp
from wispralt_server.middleware import openai_errors
from wispralt_server.middleware.rate_limit import RateLimitMiddleware
from wispralt_server.ops import staging
from wispralt_server.ops.env_writer import find_env_path
from wispralt_server.routes import (
    admin,
    admin_data,
    admin_ui,
    dev_faults,
    dictate,
    health,
)
from wispralt_server.routes import dictate_stream as dictate_stream_routes
from wispralt_server.routes import me as me_routes
from wispralt_server.routes import meeting as meeting_routes
from wispralt_server.routes import telemetry as telemetry_routes
from wispralt_server.routes import transcribe_file as transcribe_file_routes
from wispralt_server.routes import v1_models, v1_transcriptions
from wispralt_server.smart_format.mercury_client import MercuryClient
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

    # Verify the watcher's exception coverage. asyncpg has NO common base
    # class for the two failure modes we need to catch: PostgresError
    # (server-side) and InterfaceError ("pool is closed", client-side).
    # Both inherit directly from Exception via separate private hierarchies
    # (verified asyncpg 0.31.0). The watcher and route catches enumerate
    # both explicitly; this assertion confirms both class names still exist
    # at module top-level. A future asyncpg rename/unexport will fail boot
    # loudly instead of silently regressing the watcher to the 2026-05-17 bug.
    assert hasattr(asyncpg, "PostgresError") and hasattr(asyncpg, "InterfaceError"), (
        "asyncpg taxonomy changed — PostgresError or InterfaceError no longer "
        "exposed at the package top-level. Audit every "
        "`except (asyncpg.PostgresError, asyncpg.InterfaceError)` site and "
        "re-verify the watcher path covers both. See 2026-05-17 postmortem."
    )

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

    # 4a. Sweep stale chunked-upload directories (>1 h). Separate TTL from the
    #     plain-WAV sweep because abandoned chunked dirs pin a lot of disk per
    #     item; meta.json mtime is bumped on every chunk write so active
    #     uploads are safe.
    chunked_removed = staging.sweep_chunked(
        settings.staging_dir,
        max_age_seconds=3600,
    )
    if chunked_removed:
        logger.info(
            "Startup chunked sweep removed %d stale chunked upload dir(s).",
            chunked_removed,
        )

    # 4b. Sweep stale .tmp files in meeting output dir (I8).
    tmp_removed = sweep_stale_tmp(settings.meeting_output_dir)
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

    # 5. Meeting runner — re-enqueue any pending jobs that survived the restart.
    meeting_runner = MeetingRunner(job_store)
    app.state.meeting_runner = meeting_runner
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

    # 6b. Mercury client — fail-soft. If init throws, smart formatting is silently
    #     disabled. Same fail-soft contract as runtime: any error → None return.
    app.state.mercury_client = None
    if settings.openrouter_api_key:
        try:
            app.state.mercury_client = MercuryClient(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                base_url=settings.openrouter_base_url,
                timeout_ms=settings.openrouter_timeout_ms,
                app_title=settings.openrouter_app_title,
                min_words=settings.smart_format_min_words,
            )
            logger.info(
                "mercury_client initialized model=%s timeout_ms=%d",
                settings.openrouter_model,
                settings.openrouter_timeout_ms,
            )
        except Exception:
            logger.exception(
                "mercury_client init failed — smart formatting will be disabled"
            )
            app.state.mercury_client = None
    else:
        logger.info("mercury_client not configured (OPENROUTER_API_KEY unset)")

    # 6c. InsightsClient — fail-soft mirror of mercury_client. None when
    #     OPENROUTER_API_KEY is unset; weekly cron is a no-op in that case.
    #     Different model + longer timeout than mercury (batch, not live).
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

    # 7. Install compat shims at startup so the deep-patch over sys.modules hits
    #    pyannote/torch references before any user code runs. The shim
    #    is idempotent and re-invoked from pipeline._ensure_models_loaded() to
    #    close the long window between startup and first meeting.
    install_compat_shims()

    # 7b. Re-enqueue any pending jobs from prior runs. The first one to execute
    #     will lazy-load mlx-whisper + Pyannote inside the executor thread.
    try:
        await meeting_runner.reenqueue_pending()
    except Exception:  # noqa: BLE001 — never let re-enqueue crash startup
        logger.exception("Meeting reenqueue_pending failed; continuing startup")

    # 7c. Idle-eviction background task. Every minute, ask the pipeline to
    #     unload meeting models if they've been idle past the configured
    #     threshold (default 300s). Skipped entirely when threshold is 0.
    from wispralt_server.meeting import pipeline as meeting_pipeline

    app.state.eviction_task = None
    if settings.meeting_idle_eviction_seconds > 0:
        async def _eviction_loop() -> None:
            threshold = float(settings.meeting_idle_eviction_seconds)
            while not getattr(app.state, "shutting_down", False):
                try:
                    await asyncio.sleep(60.0)
                    if meeting_runner.active:
                        continue
                    evicted = await asyncio.to_thread(
                        meeting_pipeline.evict_if_idle, threshold
                    )
                    if evicted:
                        logger.info(
                            "Idle eviction released meeting models (idle threshold %.0fs).",
                            threshold,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Idle-eviction loop iteration failed")
        app.state.eviction_task = asyncio.create_task(_eviction_loop())

    # 7d. Phase 1 transcript-storage: daily sweep loop. Re-runs the same
    #     `sweep_transcripts` that ran at startup so a long-uptime server
    #     eventually reaps. Daemon-loop pattern: broad-except + log so a
    #     transient SQLite error never kills the task.
    async def _daily_transcript_sweep() -> None:
        while not getattr(app.state, "shutting_down", False):
            try:
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            try:
                j, d = job_store.sweep_transcripts(
                    days=settings.transcript_retention_days
                )
                if j or d:
                    logger.info(
                        "Daily transcript sweep: jobs=%d, dictations=%d", j, d
                    )
            except Exception:  # noqa: BLE001 — daemon loop must never crash
                logger.exception(
                    "Daily transcript sweep failed; retrying tomorrow"
                )

    app.state.transcript_sweep_task = asyncio.create_task(
        _daily_transcript_sweep()
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
    #    NOT `os._exit(0)`.  `os._exit` skips the lifespan teardown — which
    #    means the usage-event drainer's final flush is lost and any queued
    #    events are dropped.  We instead let uvicorn run its normal shutdown
    #    path (which invokes our lifespan ``finally`` block) and trust
    #    launchd's ExitTimeout=15 to bound the wait.
    def _sigterm_handler() -> None:
        logger.info("SIGTERM received — initiating graceful shutdown.")
        app.state.shutting_down = True
        try:
            n = job_store.fail_running_jobs("server shutdown")
            if n:
                logger.info("Marked %d running job(s) as failed on SIGTERM.", n)
        except (OSError, RuntimeError) as exc:
            logger.warning("Could not mark running jobs failed on SIGTERM: %s", exc)

        # Trigger uvicorn graceful shutdown so the lifespan teardown runs
        # (drainer cancel + usage flush + db.close_pool).  Fallback to raising
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
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, db.PostgresUnavailable):
        logger.exception(
            "Postgres unavailable at startup; only break-glass admin will work"
        )

    # 9b. Pool watcher — every 10s, probe the pool with SELECT 1 and recreate
    #     if dead. Without this, a transient Supabase blip leaves db_pool=None
    #     forever and every authenticated request returns 503 until the
    #     operator restarts the server. Triggered by a real outage 2026-05-02.
    app.state.db_watcher_task = None

    async def _db_watcher_loop() -> None:
        while not getattr(app.state, "shutting_down", False):
            try:
                await asyncio.sleep(10.0)
                pool = getattr(app.state, "db_pool", None)
                healthy = pool is not None and await db.health_check(pool)
                if healthy:
                    continue
                logger.warning(
                    "db_watcher: pool unhealthy (pool=%s) — attempting rebuild",
                    "None" if pool is None else "dead",
                )
                try:
                    new_pool = await db.recreate_pool()
                except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, db.PostgresUnavailable):
                    logger.exception("db_watcher: rebuild failed; will retry in 10s")
                    continue
                app.state.db_pool = new_pool
                # Restart the usage drainer against the new pool. Cancel the
                # old one (if any) — it's holding a reference to the dead pool
                # and will throw on next acquire.
                old_drainer = getattr(app.state, "usage_drainer", None)
                if old_drainer is not None and not old_drainer.done():
                    old_drainer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await old_drainer
                app.state.usage_drainer = asyncio.create_task(
                    usage_writer.drain_loop(observability.usage_queue, new_pool)
                )
                logger.info("db_watcher: pool rebuilt; usage drainer restarted")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — watcher must never crash silently
                # POSTMORTEM 2026-05-17: do NOT narrow this catch. It is the
                # last-resort guard. The bug we fixed was that asyncpg.InterfaceError
                # ("pool is closed") slipped past health_check's narrow
                # asyncpg.PostgresError catch and got swallowed HERE — silently
                # looping for 5 hours. The right fix was to broaden the INNER
                # catches (f5178be), not to make this outer one type-specific.
                # Any new code added inside the loop MUST catch its own
                # exceptions narrowly so this catch-all doesn't mask new bug shapes.
                logger.exception("db_watcher: unexpected error; continuing loop")

    app.state.db_watcher_task = asyncio.create_task(_db_watcher_loop())

    # Initialize per-token rate-limit state for /v1/* routes. Idempotent.
    # Imported locally to avoid a top-of-module cycle with the auth/users modules.
    from wispralt_server.ratelimit_per_token import init_rate_limit_state

    init_rate_limit_state(app)

    # ── yield — server is live ─────────────────────────────────────────────────
    yield

    # ── shutdown ──────────────────────────────────────────────────────────────

    # Phase 1 transcript-storage: drain in-flight dictation persists FIRST so
    # the SQLite write-lock isn't being released mid-INSERT below. The 2 s
    # budget is total across all pending persists (gather waits for ALL),
    # not 2 s each; under a SIGTERM burst some tasks may be abandoned and
    # the warning log records the count. insert_dictation is SQLite-only
    # today — placing the drain first keeps the teardown rule clear if a
    # future persist path ever adds Postgres reads.
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

    # Cancel the db pool watcher first so it stops trying to probe / rebuild
    # while the rest of shutdown is tearing the pool down.
    db_watcher: asyncio.Task | None = getattr(app.state, "db_watcher_task", None)
    if db_watcher is not None and not db_watcher.done():
        db_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await db_watcher

    # Cancel the idle-eviction loop first so it stops emitting log noise.
    eviction_task: asyncio.Task | None = getattr(app.state, "eviction_task", None)
    if eviction_task is not None and not eviction_task.done():
        eviction_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await eviction_task

    # Cancel the usage-event drainer so its CancelledError handler can
    # do the final flush of any pending events.  getattr is defensive —
    # the lifespan pre-sets the attr, so we only fall through if the
    # startup branch was bypassed entirely.
    drainer: asyncio.Task | None = getattr(app.state, "usage_drainer", None)
    if drainer is not None:
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer

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

    mercury_client: MercuryClient | None = getattr(app.state, "mercury_client", None)
    if mercury_client is not None:
        try:
            await mercury_client.aclose()
        except Exception as exc:
            logger.warning("mercury_client.aclose failed: %s", exc)

    insights_client: InsightsClient | None = getattr(
        app.state, "insights_client", None
    )
    if insights_client is not None:
        try:
            await insights_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("insights_client.aclose failed: %s", exc)

    await db.close_pool()

    logger.info("WisprAlt server clean shutdown.")


# ── application factory ───────────────────────────────────────────────────────


# Track only request-creating endpoints, NOT status-poll GETs that the
# client may hammer every few seconds.  /transcribe/meeting POST creates a
# job; /transcribe/meeting/{id} GET is the poll path — exclude it by also
# filtering on request.method.
TRACKED_ROUTES = frozenset([
    "transcribe/dictate",
    "transcribe/meeting",
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
    "transcribe/meeting": "meeting",
    "transcribe/file": "file",
    "v1/audio": "v1_dictate",
    "v1/models": "v1_models",
    "v1/audio/translations": "v1_translations",
}


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
                    kind=_KIND_MAP.get(route_key, route_key.split("/")[-1]),
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
            "mlx-whisper + Pyannote + DeepFilterNet for meeting transcription."
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

    # CORS — OUTERMOST middleware. MUST be the final app.add_middleware call:
    # Starlette wraps middleware in LIFO order, so last-added runs first on the
    # request path and last on the response path. This guarantees EVERY response
    # (including rate-limit 429 envelopes from RateLimitMiddleware above) carries
    # Access-Control-Allow-Origin so browser clients see real errors instead of
    # opaque CORS failures. Imported locally to keep the top-of-module import
    # block focused on hot-path dependencies.
    from wispralt_server.middleware.cors import install_cors

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
    # /transcribe/dictate. CRITICAL ORDERING: registered BEFORE dev_faults
    # below so the production handlers win when no `?fault=` query param is
    # set (mirrors the legacy /transcribe/dictate vs dev_faults precedence).
    app.include_router(dictate_stream_routes.router)
    # /admin/* legacy JSON endpoints (rotate-key, /metrics) — auth per-route.
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
    # /transcribe/meeting — Phase 2 meeting endpoints.
    app.include_router(meeting_routes.router)
    # /transcribe/file — container-agnostic submission (ffmpeg-transcoded).
    app.include_router(transcribe_file_routes.router)
    # /me — JSON identity self-management (any authenticated role).
    app.include_router(me_routes.router)
    # /telemetry/cloud-dictation — Swift client cloud-fallback dictation sync.
    #     Bearer-auth (cookie-only rejected). Rate-limited 10 batches/min/IP via
    #     RateLimitMiddleware.
    app.include_router(telemetry_routes.router)
    # /v1/audio/transcriptions — OpenAI-compat shim.
    app.include_router(v1_transcriptions.router)
    # /v1/models — OpenAI-compat static model list. Open WebUI + enterprise
    # clients probe this before transcribing.
    app.include_router(v1_models.router)

    # Dev-only fault injection. Mounted ONLY when WISPRALT_DEV_FAULTS=1 AND
    # the host is non-prod. Used to verify the Swift client's offline-signature
    # classifier never trips on origin 5xx-with-X-Request-Id.
    if dev_faults.is_dev_faults_enabled():
        logger.warning(
            "Mounting dev_faults router (WISPRALT_DEV_FAULTS=1). "
            "MUST NOT be enabled on prod-mini."
        )
        app.include_router(dev_faults.router)

    # Re-shape errors on /v1/* paths to OpenAI envelope. Native routes keep their
    # default {"detail": ...} shape. Must run AFTER include_router calls.
    openai_errors.install(app)

    return app


app = create_app()


# M4: _locate_env removed — use ops.env_writer.find_env_path() instead.
