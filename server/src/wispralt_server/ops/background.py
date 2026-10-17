"""
ops/background.py — long-lived asyncio daemon loops started by the lifespan.

Moved verbatim-in-spirit from main.py. Each loop is a coroutine taking the
FastAPI ``app`` (for ``app.state`` access) and never lets a transient error
kill the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Callable

import asyncpg
from fastapi import FastAPI

from wispralt_server import db, observability
from wispralt_server.config import settings
from wispralt_server.jobs.store import JobStore
from wispralt_server.meeting.output import remove_job_outputs, sweep_stale_tmp
from wispralt_server.ops import staging
from wispralt_server.ops.logging_setup import RepeatedFailureLogger
from wispralt_server.usage import writer as usage_writer

logger = logging.getLogger(__name__)

_DAILY_S = 24 * 3600


def sweep_staging(job_store: JobStore) -> tuple[int, int, int]:
    """Run the staging / chunked / .tmp sweeps. Returns the three removal counts.

    WAVs referenced by pending or running jobs are excluded (C7 fix).
    """
    active_wav_paths = {Path(j.wav_path) for j in job_store.list_active_jobs()}
    return (
        staging.sweep_old(
            settings.staging_dir,
            max_age_seconds=86400,
            exclude_paths=active_wav_paths,
        ),
        staging.sweep_chunked(settings.staging_dir, max_age_seconds=3600),
        sweep_stale_tmp(settings.meeting_output_dir),
    )


def sweep_old_meetings(
    job_store: JobStore,
    days: int,
    output_dir: Path,
    is_active: Callable[[str], bool] | None = None,
) -> int:
    """Delete finished job rows older than *days* plus their output files.

    ``days <= 0`` disables. Safeguards:

    - Jobs the runner still tracks as active (*is_active*) are skipped: the
      phase watchdog can mark a job ``failed`` while its executor thread is
      still running.
    - Files are removed from each job's persisted ``output_dir``, which must
      resolve inside *output_dir* (the allowed root); otherwise no files are
      touched for that job (path-traversal guard).
    - If any file fails to delete, the row is kept so the next sweep retries.

    Returns the number of rows deleted.
    """
    if days <= 0:
        return 0
    root = output_dir.resolve()
    deletable: list[str] = []
    for jid, job_dir in job_store.list_expired_finished_jobs(days):
        if is_active is not None and is_active(jid):
            continue
        target = Path(job_dir).resolve() if job_dir else root
        if not target.is_relative_to(root):
            logger.warning(
                "Retention: job %s output_dir %s is outside %s; not touching files",
                jid, job_dir, root,
            )
        elif not remove_job_outputs(target, jid):
            continue
        deletable.append(jid)
    return job_store.delete_finished_jobs(deletable) if deletable else 0


def run_daily_sweeps(
    job_store: JobStore, is_active: Callable[[str], bool] | None = None
) -> None:
    """One pass of every daily maintenance sweep (blocking; run in a thread)."""
    wavs, chunked, tmp = sweep_staging(job_store)
    if wavs or chunked or tmp:
        logger.info(
            "Daily staging sweep: wavs=%d, chunked_dirs=%d, tmp=%d",
            wavs, chunked, tmp,
        )
    j, d = job_store.sweep_transcripts(days=settings.transcript_retention_days)
    if j or d:
        logger.info("Daily transcript sweep: jobs=%d, dictations=%d", j, d)
    purged = sweep_old_meetings(
        job_store, settings.meeting_job_retention_days, settings.meeting_output_dir,
        is_active,
    )
    if purged:
        logger.info("Daily meeting retention sweep: jobs deleted=%d", purged)


async def daily_sweep_loop(
    app: FastAPI, job_store: JobStore, is_active: Callable[[str], bool] | None = None
) -> None:
    """Re-run staging, transcript and meeting-retention sweeps every 24 h.

    Daemon-loop pattern: broad-except + log so a transient SQLite/OS error
    never kills the task.
    """
    while not getattr(app.state, "shutting_down", False):
        try:
            await asyncio.sleep(_DAILY_S)
        except asyncio.CancelledError:
            raise
        try:
            await asyncio.to_thread(run_daily_sweeps, job_store, is_active)
        except Exception:  # noqa: BLE001 — daemon loop must never crash
            logger.exception("Daily sweep failed; retrying tomorrow")


async def eviction_loop(app: FastAPI, meeting_runner, meeting_pipeline) -> None:
    """Release meeting models after ``meeting_idle_eviction_seconds`` idle."""
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


async def db_watcher_loop(
    app: FastAPI,
    interval_s: float = 10.0,
    summary_interval_s: float = 300.0,
) -> None:
    """Probe the Postgres pool every *interval_s* and rebuild it if dead.

    Without this, a transient Supabase blip leaves db_pool=None forever and
    every authenticated request returns 503 until the operator restarts the
    server (real outage 2026-05-02). Repeated identical failures are logged
    once in full, then summarised every *summary_interval_s* (a past outage
    produced ~23,000 identical "pool is closed" tracebacks).
    """
    # One limiter per failure kind: sharing one would flip keys every
    # iteration (unhealthy -> rebuild-failed -> unhealthy ...) and defeat dedupe.
    unhealthy_log = RepeatedFailureLogger(logger, summary_interval_s)
    rebuild_log = RepeatedFailureLogger(logger, summary_interval_s)
    unexpected_log = RepeatedFailureLogger(logger, summary_interval_s)
    while not getattr(app.state, "shutting_down", False):
        try:
            await asyncio.sleep(interval_s)
            pool = getattr(app.state, "db_pool", None)
            healthy = pool is not None and await db.health_check(pool)
            if healthy:
                unhealthy_log.recovered()
                rebuild_log.recovered()
                unexpected_log.recovered()
                continue
            unhealthy_log.failure(
                "pool-unhealthy",
                "db_watcher: pool unhealthy (pool=%s) — attempting rebuild",
                "None" if pool is None else "dead",
            )
            try:
                new_pool = await db.recreate_pool()
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, db.PostgresUnavailable) as exc:
                rebuild_log.failure(
                    f"rebuild-failed:{type(exc).__name__}:{exc}",
                    "db_watcher: rebuild failed; will retry in %.0fs",
                    interval_s,
                    exc_info=True,
                )
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
            unhealthy_log.recovered()
            rebuild_log.recovered()
            logger.info("db_watcher: pool rebuilt; usage drainer restarted")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watcher must never crash silently
            # POSTMORTEM 2026-05-17: do NOT narrow this catch. It is the
            # last-resort guard. The bug we fixed was that asyncpg.InterfaceError
            # ("pool is closed") slipped past health_check's narrow
            # asyncpg.PostgresError catch and got swallowed HERE — silently
            # looping for 5 hours. The right fix was to broaden the INNER
            # catches (f5178be), not to make this outer one type-specific.
            # Any new code added inside the loop MUST catch its own
            # exceptions narrowly so this catch-all doesn't mask new bug shapes.
            unexpected_log.failure(
                f"unexpected:{type(exc).__name__}:{exc}",
                "db_watcher: unexpected error; continuing loop",
                exc_info=True,
            )
