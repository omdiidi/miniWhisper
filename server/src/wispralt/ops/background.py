"""
ops/background.py — long-lived asyncio daemon loops started by the lifespan.

Moved verbatim-in-spirit from main.py. Each loop is a coroutine taking the
FastAPI ``app`` (for ``app.state`` access) and never lets a transient error
kill the task.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI

from wispralt.config import settings
from wispralt.files.output import remove_job_outputs, sweep_stale_tmp
from wispralt.jobs.store import JobStore
from wispralt.ops import staging

logger = logging.getLogger(__name__)

_DAILY_S = 24 * 3600


def sweep_staging(job_store: JobStore) -> tuple[int, int, int]:
    """Run the staging / chunked / .tmp sweeps. Returns the three removal counts.

    WAVs referenced by pending or running jobs are excluded (C7 fix).
    """
    active_wav_paths = {j.wav_path for j in job_store.list_active_jobs()}
    return (
        staging.sweep_old(
            settings.staging_dir,
            max_age_seconds=86400,
            exclude_paths=active_wav_paths,
        ),
        staging.sweep_chunked(settings.staging_dir, max_age_seconds=3600),
        sweep_stale_tmp(settings.outputs_dir),
    )


def sweep_old_meetings(
    job_store: JobStore,
    days: int,
    output_dir: Path,
    is_active: Callable[[str], bool] | None = None,
) -> int:
    """Delete finished job rows older than *days* plus their output files in *output_dir*.

    ``days <= 0`` disables. Jobs the runner still tracks as active
    (*is_active*) are skipped (the watchdog can mark a job ``failed`` while
    its executor thread still runs). If any file fails to delete, the row is
    kept so the next sweep retries. Returns the number of rows deleted.
    """
    if days <= 0:
        return 0
    deletable = [
        jid
        for jid in job_store.list_expired_finished_jobs(days)
        if not (is_active is not None and is_active(jid)) and remove_job_outputs(output_dir, jid)
    ]
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
        job_store, settings.meeting_job_retention_days, settings.outputs_dir,
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


async def eviction_loop(app: FastAPI, file_runner, files_pipeline) -> None:
    """Release the file model after ``file_model_idle_eviction_seconds`` idle."""
    threshold = float(settings.file_model_idle_eviction_seconds)
    while not getattr(app.state, "shutting_down", False):
        try:
            await asyncio.sleep(60.0)
            if file_runner.active:
                continue
            evicted = await asyncio.to_thread(
                files_pipeline.evict_if_idle, threshold
            )
            if evicted:
                logger.info(
                    "Idle eviction released the file model (idle threshold %.0fs).",
                    threshold,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Idle-eviction loop iteration failed")
