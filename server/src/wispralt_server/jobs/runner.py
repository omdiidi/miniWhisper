"""
jobs/runner.py — In-process meeting transcription runner.

Design decisions (v3 deltas P4#3 + P5#1 + P5#11):
- One job at a time: asyncio.Semaphore(1) prevents concurrent submissions.
- Dedicated ThreadPoolExecutor(max_workers=1): keeps meeting work isolated from
  the default asyncio thread pool used by other I/O tasks.
- OOM guard: reject a new job if less than 2 GiB RAM is available before
  starting (the meeting pipeline needs ~7 GB for all models + inference).
- Staging WAV is always cleaned up in the ``finally`` block (R1#2), regardless
  of success or failure.
- reenqueue_pending(): called at startup after recover_orphans() to re-run any
  pending jobs whose WAV files survived the previous restart.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from .._errors import MeetingInProgressError
from ..config import settings
from ..meeting import pipeline as meeting_pipeline
from ..ops import staging
from .store import JobStore

logger = logging.getLogger(__name__)

_2GiB = 2 * 1024**3


class MeetingRunner:
    """Single-meeting-at-a-time runner backed by a dedicated thread executor.

    Lifecycle::

        runner = MeetingRunner(store)
        jid = runner.submit_or_429(wav_path)   # raises MeetingInProgressError if busy
        # ... poll store.get(jid).status ...

    At startup, after ``JobStore.recover_orphans()``::

        await runner.reenqueue_pending()
    """

    def __init__(self, store: JobStore) -> None:
        self.store = store
        # Dedicated single-thread pool — isolates heavy meeting I/O + CPU from
        # the default pool used by asyncio.to_thread for lighter tasks.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wispralt-meeting"
        )
        # Semaphore enforces at-most-one concurrent meeting job.
        self._semaphore = asyncio.Semaphore(1)
        self._active_job_id: str | None = None

    # ── public properties ──────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """True while a meeting job is currently running."""
        return self._semaphore.locked()

    @property
    def active_job_id(self) -> str | None:
        """Job ID of the currently running job, or None."""
        return self._active_job_id

    # ── public methods ─────────────────────────────────────────────────────────

    def submit_or_429(self, wav_path: Path) -> str:
        """Create a job and fire-and-forget its execution coroutine.

        Raises MeetingInProgressError (which the route converts to HTTP 429) if:
        - A job is already running (semaphore locked), OR
        - Available RAM is below 2 GiB (P5#11 OOM guard).

        Returns the new job's UUID.
        """
        if self._semaphore.locked():
            raise MeetingInProgressError(
                "Another meeting is currently being transcribed"
            )
        if psutil.virtual_memory().available < _2GiB:
            raise MeetingInProgressError(
                "Insufficient memory to start a new meeting job; try again later"
            )

        jid = self.store.create(str(wav_path))
        # Schedule as a background task; the route handler returns immediately.
        asyncio.create_task(self._run(jid, wav_path))
        return jid

    async def reenqueue_pending(self) -> None:
        """Re-enqueue pending jobs whose WAV files survived the last restart.

        Called once at startup after ``JobStore.recover_orphans()`` returns its
        "requeue" list.  Each job is submitted as a new asyncio task; the
        semaphore ensures they run one at a time.
        """
        pending_ids = self._get_pending_ids()
        if pending_ids:
            logger.info(
                "Re-enqueueing %d pending job(s) from last run: %s",
                len(pending_ids),
                pending_ids,
            )
        for jid in pending_ids:
            job = self.store.get(jid)
            if job is None:
                continue
            asyncio.create_task(self._run(job.id, Path(job.wav_path)))

    # ── private ───────────────────────────────────────────────────────────────

    async def _run(self, jid: str, wav_path: Path) -> None:
        """Acquire the semaphore, run the pipeline in the executor, update store."""
        async with self._semaphore:
            self._active_job_id = jid
            try:
                self.store.set_running(jid)
                logger.info("[%s] Meeting job started.", jid)

                loop = asyncio.get_running_loop()
                transcript = await loop.run_in_executor(
                    self._executor,
                    meeting_pipeline.transcribe_meeting,
                    wav_path,
                    settings.meeting_output_dir,
                    jid,
                    settings.silence_threshold,
                )
                mode: str = transcript.get("mode", "unknown")
                self.store.set_done(jid, mode, str(settings.meeting_output_dir))
                logger.info("[%s] Meeting job done (mode=%s).", jid, mode)

            except Exception as exc:
                error_repr = repr(exc)
                self.store.set_failed(jid, error_repr)
                logger.exception("[%s] Meeting job failed: %s", jid, error_repr)

            finally:
                self._active_job_id = None
                # Always clean up the staging WAV (R1#2).
                staging.cleanup(wav_path)

    def _get_pending_ids(self) -> list[str]:
        """Fetch all pending job IDs directly from the SQLite connection."""
        with self.store._lock:
            cur = self.store.con.execute(
                "SELECT id FROM jobs WHERE status='pending'"
            )
            return [row[0] for row in cur.fetchall()]
