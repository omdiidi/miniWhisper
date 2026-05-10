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
import functools
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
        # C8: submit lock prevents TOCTOU race between the semaphore check,
        # store.create, and asyncio.create_task.  All three must be atomic.
        self._submit_lock = asyncio.Lock()

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

    async def submit_or_429(self, wav_path: Path) -> str:
        """Create a job and fire-and-forget its execution coroutine.

        Raises MeetingInProgressError (which the route converts to HTTP 429) if:
        - A job is already running (semaphore locked), OR
        - Available RAM is below 2 GiB (P5#11 OOM guard).

        Returns the new job's UUID.

        C8: the semaphore check, store.create, and asyncio.create_task all run
        under ``_submit_lock`` to prevent a TOCTOU race where two concurrent
        requests both observe `locked() == False` and both create tasks.
        """
        async with self._submit_lock:
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
            asyncio.create_task(self._run_pipeline(jid, wav_path))
            return jid

    async def submit_source_or_429(self, src_path: Path) -> str:
        """Like :meth:`submit_or_429` but the input is a NOT-YET-TRANSCODED source.

        Used by /transcribe/file: the source is staged in its original
        container; the worker (:meth:`_run_source`) runs ffprobe + ffmpeg
        before handing the canonical WAV to the existing pipeline.

        Mirrors :meth:`submit_or_429` exactly: same ``_submit_lock`` +
        semaphore.locked() + RAM-availability check + create + create_task
        sequence so the at-most-one-job invariant is preserved across both
        submission paths.
        """
        async with self._submit_lock:
            if self._semaphore.locked():
                raise MeetingInProgressError(
                    "Another meeting is currently being transcribed"
                )
            if psutil.virtual_memory().available < _2GiB:
                raise MeetingInProgressError(
                    "Insufficient memory to start a new meeting job; try again later"
                )

            # wav_path holds the SOURCE container until the worker transcodes
            # it and calls store.update_after_transcode. Discriminator for
            # routing on restart is the file extension (.wav vs other).
            jid = self.store.create(str(src_path))
            asyncio.create_task(self._run_source(jid, src_path))
            return jid

    async def reenqueue_pending(self) -> None:
        """Re-enqueue pending jobs whose WAV files survived the last restart.

        Called once at startup after ``JobStore.recover_orphans()`` returns its
        "requeue" list.  Each job is submitted as a new asyncio task; the
        semaphore ensures they run one at a time.

        C9: Jobs with attempts >= 3 are skipped and immediately failed to prevent
        unbounded poison-pill re-enqueue loops.
        """
        pending_ids = self.store.list_pending_ids()  # I7: use public API
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
            # C9: cap retries at 3 to prevent poison-pill loops.
            if job.attempts >= 3:
                logger.warning(
                    "[%s] Skipping re-enqueue: job has %d attempts (max 3). Marking failed.",
                    jid,
                    job.attempts,
                )
                self.store.set_failed(jid, "max retries exceeded")
                continue
            # Route by file extension. .wav → already-transcoded → pipeline.
            # Anything else → /transcribe/file source that crashed before
            # transcode → re-run from source.
            ext = Path(job.wav_path).suffix.lower()
            if ext == ".wav":
                asyncio.create_task(self._run_pipeline(
                    job.id,
                    Path(job.wav_path),
                    force_single_channel=bool(job.force_single_channel),
                ))
            else:
                asyncio.create_task(self._run_source(job.id, Path(job.wav_path)))

    # ── private ───────────────────────────────────────────────────────────────

    async def _run_pipeline(
        self,
        jid: str,
        wav_path: Path,
        *,
        force_single_channel: bool = False,
    ) -> None:
        """Acquire the semaphore, run the pipeline in the executor, update store.

        Renamed from ``_run``. ``force_single_channel`` defaults to False so
        the legacy /transcribe/meeting path (which always submits a 2-channel
        WAV) is unchanged.

        Round-2 R2#1: this method is the legacy entry point (e.g. the meeting
        path that already has a canonical WAV on disk). It acquires the
        semaphore around :meth:`_run_pipeline_inner`. The /transcribe/file
        path goes through :meth:`_run_source`, which acquires the semaphore
        ITSELF for the full ffprobe + ffmpeg + pipeline window and calls
        ``_run_pipeline_inner`` directly to avoid double-acquisition.
        """
        async with self._semaphore:
            await self._run_pipeline_inner(
                jid, wav_path, force_single_channel=force_single_channel
            )

    async def _run_pipeline_inner(
        self,
        jid: str,
        wav_path: Path,
        *,
        force_single_channel: bool = False,
    ) -> None:
        """Pipeline body without semaphore acquisition.

        The caller MUST hold ``self._semaphore`` for the duration of this
        call. Used by both :meth:`_run_pipeline` (which acquires the
        semaphore) and :meth:`_run_source` (which acquires the semaphore
        across the broader ffprobe + ffmpeg + pipeline window).
        """
        self._active_job_id = jid
        try:
            self.store.set_running(jid)
            logger.info("[%s] Meeting job started.", jid)

            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                self._executor,
                functools.partial(
                    meeting_pipeline.transcribe_meeting,
                    wav_path,
                    settings.meeting_output_dir,
                    jid,
                    settings.silence_threshold,
                    force_single_channel=force_single_channel,
                ),
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

    async def _run_source(self, jid: str, src_path: Path) -> None:
        """Worker for /transcribe/file submissions.

        Sequence: ffprobe → ffmpeg transcode → existing pipeline. Status uses
        the existing ``running`` value (set via ``set_running``) — no new
        ``transcoding`` status (would require updating every status consumer).

        Acceptable rare-edge: if we crash AFTER the canonical WAV is on disk
        but BEFORE update_after_transcode commits, the row still points at
        ``src_path``; the next ``recover_orphans`` re-runs the source through
        ffmpeg. The earlier canonical WAV is left for ``staging.sweep_old`` to
        garbage-collect.

        Round-2 R2#1: the semaphore is acquired around the FULL body
        (ffprobe + ffmpeg + pipeline) so that two concurrent
        ``submit_source_or_429`` callers cannot both pass the
        ``_semaphore.locked()`` check and both start ffmpeg simultaneously.
        The pipeline phase calls :meth:`_run_pipeline_inner` directly to
        avoid double-acquiring the semaphore.
        """
        async with self._semaphore:
            try:
                self.store.set_running(jid)
                logger.info("[%s] File job started: %s", jid, src_path)

                # 1. Detect channel count of source.
                channel_count = staging.ffprobe_channel_count(src_path)
                force_single = (channel_count == 1)
                target_channels = 1 if force_single else 2

                # 2. ffmpeg-transcode to canonical WAV. subprocess.run blocks;
                #    run it on the dedicated meeting executor so it shares the
                #    same RAM-gated worker as the pipeline.
                loop = asyncio.get_running_loop()
                wav_path = await loop.run_in_executor(
                    self._executor,
                    functools.partial(
                        staging.transcode_to_canonical_wav,
                        src_path,
                        target_channels=target_channels,
                    ),
                )

                # 3. Durability boundary: persist the canonical wav_path + flag
                #    BEFORE deleting the source. Once committed, it's safe to
                #    delete the source because the row points at the new wav.
                self.store.update_after_transcode(
                    jid,
                    wav_path=str(wav_path),
                    force_single_channel=force_single,
                )
                src_path.unlink(missing_ok=True)

                # 4. Run the pipeline body (semaphore already held — call the
                #    inner helper to avoid double-acquisition).
                await self._run_pipeline_inner(
                    jid,
                    wav_path,
                    force_single_channel=force_single,
                )

            except Exception as exc:
                # R2#2: only call set_failed if _run_pipeline_inner did NOT
                # already mark the job terminal. Otherwise we overwrite a
                # more precise pipeline failure (or, worse, flip a `done`
                # job to `failed`).
                error_repr = repr(exc)
                job = self.store.get(jid)
                if job is None or job.status not in ("done", "failed"):
                    self.store.set_failed(jid, error_repr)
                logger.exception(
                    "[%s] File job failed pre-pipeline: %s", jid, error_repr
                )
                src_path.unlink(missing_ok=True)

