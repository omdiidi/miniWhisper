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

Phase 2 + 3 deltas:
- ``ProcessingMode`` enum encodes submit-time intent (file | meeting).
- ``request_mode`` column on the jobs row is the source of truth for
  diarization routing — replaces the ad-hoc ``channel_count == 1 → mono``
  heuristic that misclassified mono meetings as single-speaker.
- ``_phase()`` wraps the seams that ARE wrappable (ffprobe + ffmpeg_decode):
  real ``asyncio.wait_for`` timeouts, real abort. The in-pipeline phases
  (transcribe / diarize / merge / output_write) cannot be interrupted because
  the executor thread is owned by mlx-whisper; per-phase budgets there are
  enforced by ``_phase_watchdog`` which is HONEST about its limitations: it
  marks the row failed for UI purposes but does NOT release the semaphore.
  New submissions still 429 until the executor returns naturally.
- ``cancel_cb`` is wired into ``staging.transcode_to_canonical_wav`` (Popen
  poll loop) so cancel-mid-decode works cleanly.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

import psutil

from .._errors import MeetingInProgressError
from ..config import settings
from ..meeting import pipeline as meeting_pipeline
from ..ops import staging
from .store import JobStore

logger = logging.getLogger(__name__)

_2GiB = 2 * 1024**3


class ProcessingMode(str, Enum):
    """Submit-time intent for a transcription job.

    - ``FILE``: voice memo / interview / lecture / podcast — single speaker by
      default; pipeline runs the single-channel branch and skips diarization.
    - ``MEETING``: 2-party (or N-party) meeting recording — pipeline runs the
      stereo or in-person branch with pyannote diarization.

    Distinct from the existing ``Job.mode`` column (remote | in_person | None)
    which is set by the pipeline on completion to describe what it actually
    detected. ``request_mode`` is the caller's INTENT at submit time.
    """

    FILE = "file"
    MEETING = "meeting"


# Per-phase wall budgets (seconds). Scalar entries are absolute; callable
# entries take audio duration in seconds and return a budget that scales with
# content length. The watchdog calls a budget by ``budget_s = budget(d) if
# callable(budget) else budget``.
PHASE_BUDGETS: dict[str, float | object] = {
    "ffprobe": 30,
    "ffmpeg_decode": 600,
    # Cold-start tax: first job pays MLX + pyannote load.
    "transcribe_load": 120,
    "transcribe": (lambda d: d * 4 + 120),
    "diarize_load": 120,
    "diarize": (lambda d: d * 2.0 + 60),
    "merge": 60,
    "output_write": 30,
}

# Operator-friendly labels for the client UI + admin /admin/active rich
# projection. Mirrored client-side in ``RecordingState.phaseLabel``.
PHASE_LABELS: dict[str, str] = {
    "queued": "Waiting in queue",
    "starting": "Starting",
    "ffprobe": "Inspecting audio",
    "ffmpeg_decode": "Decoding audio",
    "transcribe_load": "Loading transcription model",
    "transcribe": "Transcribing",
    "diarize_load": "Loading speaker model",
    "diarize": "Identifying speakers",
    "merge": "Finalizing transcript",
    "output_write": "Writing outputs",
}


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

        Legacy /transcribe/meeting POST entry point. Persists
        ``request_mode=MEETING`` so the row matches the route's intent
        without a route-side change.

        Raises MeetingInProgressError (which the route converts to HTTP 429) if:
        - A job is already running (semaphore locked), OR
        - Available RAM is below 2 GiB (P5#11 OOM guard).
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

            jid = self.store.create(
                str(wav_path),
                request_mode=ProcessingMode.MEETING.value,
            )
            asyncio.create_task(self._run_pipeline(jid, wav_path))
            return jid

    async def submit_source_or_429(
        self,
        src_path: Path,
        request_mode: ProcessingMode,
    ) -> str:
        """Like :meth:`submit_or_429` but the input is a NOT-YET-TRANSCODED source.

        Used by /transcribe/file: the source is staged in its original
        container; the worker (:meth:`_run_source`) runs ffprobe + ffmpeg
        before handing the canonical WAV to the existing pipeline.

        ``request_mode`` (ProcessingMode.FILE | ProcessingMode.MEETING) is
        persisted on the row and threaded through to the pipeline. It
        replaces the channel-count heuristic for deciding whether to skip
        diarization.
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

            jid = self.store.create(
                str(src_path),
                request_mode=request_mode.value,
            )
            asyncio.create_task(self._run_source(jid, src_path, request_mode))
            return jid

    async def reenqueue_pending(self) -> None:
        """Re-enqueue pending jobs whose source/WAV files survived the last restart.

        Called once at startup after ``JobStore.recover_orphans()`` returns its
        "requeue" list.  Each job is submitted as a new asyncio task; the
        semaphore ensures they run one at a time.

        Routing is by file extension AND ``request_mode``:
        - ``.wav`` extension → already-transcoded → ``_run_pipeline``.
        - any other extension → pre-transcode source → ``_run_source`` with the
          persisted ``request_mode`` (legacy rows backfilled by
          :meth:`JobStore.recover_orphans`).

        C9: Jobs with attempts >= 3 are skipped and immediately failed to
        prevent unbounded poison-pill re-enqueue loops.
        """
        rows = self.store.list_pending_ids_with_mode()
        if rows:
            logger.info(
                "Re-enqueueing %d pending job(s) from last run: %s",
                len(rows),
                [r[0] for r in rows],
            )
        for jid, wav_path, req_mode_str in rows:
            job = self.store.get(jid)
            if job is None:
                continue
            if job.attempts >= 3:
                logger.warning(
                    "[%s] Skipping re-enqueue: job has %d attempts (max 3). Marking failed.",
                    jid,
                    job.attempts,
                )
                self.store.set_failed(jid, "max retries exceeded")
                continue

            try:
                req_mode = ProcessingMode(req_mode_str or "meeting")
            except ValueError:
                logger.warning(
                    "[%s] Unknown request_mode=%r; defaulting to MEETING",
                    jid, req_mode_str,
                )
                req_mode = ProcessingMode.MEETING

            path = Path(wav_path)
            ext = path.suffix.lower()
            if ext == ".wav":
                asyncio.create_task(self._run_pipeline(
                    job.id,
                    path,
                    force_single_channel=bool(job.force_single_channel),
                ))
            else:
                asyncio.create_task(self._run_source(job.id, path, req_mode))

    # ── private ───────────────────────────────────────────────────────────────

    async def _run_pipeline(
        self,
        jid: str,
        wav_path: Path,
        *,
        force_single_channel: bool = False,
    ) -> None:
        """Acquire the semaphore, run the pipeline in the executor, update store.

        Legacy entry point (/transcribe/meeting POST already has a canonical
        WAV on disk). It acquires the semaphore around
        :meth:`_run_pipeline_inner`. The /transcribe/file path goes through
        :meth:`_run_source`, which acquires the semaphore ITSELF for the full
        ffprobe + ffmpeg + pipeline window and calls ``_run_pipeline_inner``
        directly to avoid double-acquisition.
        """
        async with self._semaphore:
            # Legacy meeting submissions know their audio duration only after
            # the pipeline runs; pass 0.0 → watchdog uses the absolute scalar
            # path of any callable budgets (3600s default in practice for
            # transcribe).  A future delta could ffprobe up front.
            await self._run_pipeline_inner(
                jid,
                wav_path,
                force_single_channel=force_single_channel,
                request_mode=ProcessingMode.MEETING,
                audio_duration_s=0.0,
            )

    async def _run_pipeline_inner(
        self,
        jid: str,
        wav_path: Path,
        *,
        force_single_channel: bool = False,
        request_mode: ProcessingMode = ProcessingMode.MEETING,
        audio_duration_s: float = 0.0,
    ) -> None:
        """Pipeline body without semaphore acquisition.

        The caller MUST hold ``self._semaphore`` for the duration of this
        call. Threads ``progress_cb`` + ``cancel_cb`` to the pipeline so the
        client UI can render per-phase progress and the user can request
        cooperative cancellation. Guards ``set_done`` against the case where
        the watchdog already wrote ``failed`` while the executor was running.
        """
        self._active_job_id = jid
        try:
            self.store.set_running(jid)
            self.store.update_phase(jid, "transcribe_load")
            logger.info("[%s] Meeting job started.", jid)

            progress_cb = lambda phase, idx, total: self._on_progress(  # noqa: E731
                jid, phase, idx, total
            )
            cancel_cb = lambda: self.store.check_cancel_requested(jid)  # noqa: E731
            # Closure called from inside the executor at each in-pipeline
            # phase boundary; freshens phase_started_at so the watchdog's
            # elapsed calc is per-phase, not per-job.
            phase_setter_cb = lambda name: self.store.update_phase(jid, name)  # noqa: E731

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
                    request_mode=request_mode.value,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    phase_setter_cb=phase_setter_cb,
                ),
            )

            # Guard: watchdog or cancel may have already set the row to
            # failed while the executor was finishing up. Don't flip a
            # failed row back to done.
            current = self.store.get(jid)
            if current is not None and current.status == "failed":
                logger.warning(
                    "[%s] executor returned post-failure (%s); discarding result",
                    jid, current.error,
                )
                return

            mode: str = transcript.get("mode", "unknown")
            self.store.set_done(jid, mode, str(settings.meeting_output_dir))
            logger.info("[%s] Meeting job done (mode=%s).", jid, mode)

        except Exception as exc:
            error_repr = repr(exc)
            current = self.store.get(jid)
            if current is None or current.status not in ("done", "failed"):
                self.store.set_failed(jid, error_repr)
            logger.exception("[%s] Meeting job failed: %s", jid, error_repr)

        finally:
            self._active_job_id = None
            # Always clean up the staging WAV (R1#2).
            staging.cleanup(wav_path)

    async def _run_source(
        self,
        jid: str,
        src_path: Path,
        request_mode: ProcessingMode,
    ) -> None:
        """Worker for /transcribe/file submissions.

        Sequence: ffprobe (channels) → ffprobe (duration) → ffmpeg transcode
        → existing pipeline. Status uses the existing ``running`` value (set
        via ``set_running``) — no new ``transcoding`` status (would require
        updating every status consumer).

        Phase markers via :meth:`_phase` make ffprobe + ffmpeg_decode
        independently observable AND independently abortable: each is its
        own ``run_in_executor`` call wrapped in ``asyncio.wait_for`` so a
        hung ffmpeg cannot eat the executor forever.

        The watchdog spawned via ``asyncio.create_task`` covers the
        in-pipeline phases (transcribe / diarize / merge / output_write).
        It is HONEST about its limitations: it sets the row to failed when a
        phase exceeds 2× budget, but does NOT release the semaphore — the
        ``async with self._semaphore`` here is awaiting the executor and
        will only exit when the executor thread returns naturally.
        New submissions will 429 with Retry-After until that happens; the
        client surfaces this as a "Previous job finishing on server" banner
        rather than a mysterious error.

        Acceptable rare-edge: if we crash AFTER the canonical WAV is on disk
        but BEFORE update_after_transcode commits, the row still points at
        ``src_path``; the next ``recover_orphans`` re-runs the source through
        ffmpeg. The earlier canonical WAV is left for ``staging.sweep_old`` to
        garbage-collect.
        """
        async with self._semaphore:
            watchdog_task: asyncio.Task[None] | None = None
            try:
                self._active_job_id = jid
                self.store.set_running(jid)
                self.store.update_phase(jid, "starting")
                logger.info(
                    "[%s] File job started: %s (request_mode=%s)",
                    jid, src_path, request_mode.value,
                )

                # Phase 1: ffprobe channels — REAL timeout, REAL abort.
                channel_count = await self._phase(
                    jid, "ffprobe", PHASE_BUDGETS["ffprobe"],
                    staging.ffprobe_channel_count, src_path,
                )

                # Audio duration for watchdog scaling. Reuse the executor for
                # a clean cancel path; budget is the same ffprobe budget.
                loop = asyncio.get_running_loop()
                audio_duration_s = await loop.run_in_executor(
                    self._executor, staging.ffprobe_duration, src_path
                )
                self.store.update_audio_duration(jid, audio_duration_s)

                # Channel target: stereo for legacy meeting compatibility (the
                # pipeline's in-person branch handles silent-ch2 correctly).
                # File-mode mono inputs: keep target_channels=1 to skip the
                # silent-ch2 handling entirely.
                if (
                    request_mode == ProcessingMode.FILE
                    and channel_count == 1
                ):
                    target_channels = 1
                else:
                    target_channels = 2

                # When MEETING mode + mono source, request that ffmpeg pad the
                # second canonical-WAV channel with explicit silence (rather
                # than duplicating ch0 into ch1). Without this, the pipeline's
                # `is_silent_robust(ch2)` returns False because ffmpeg's
                # default `-ac 2` from mono input fills BOTH channels — so the
                # remote branch fires and transcribes the audio twice, with
                # "You"/"Other" labels instead of pyannote speaker labels.
                pad_mono_silent = (
                    request_mode == ProcessingMode.MEETING
                    and channel_count == 1
                    and target_channels == 2
                )

                # Phase 2: ffmpeg_decode — REAL timeout, REAL abort, cancel-aware.
                cancel_cb = lambda: self.store.check_cancel_requested(jid)  # noqa: E731
                wav_path = await self._phase(
                    jid, "ffmpeg_decode", PHASE_BUDGETS["ffmpeg_decode"],
                    functools.partial(
                        staging.transcode_to_canonical_wav,
                        src_path,
                        target_channels=target_channels,
                        cancel_cb=cancel_cb,
                        pad_mono_to_stereo_silent=pad_mono_silent,
                    ),
                )

                # The actual fix for "all Speaker 1": derive force_single from
                # request_mode, not from channel count. A mono meeting (single
                # mic, multiple speakers) will now keep diarization enabled.
                force_single = (request_mode == ProcessingMode.FILE)

                # Durability boundary: persist canonical wav_path + flag
                # BEFORE deleting the source.
                self.store.update_after_transcode(
                    jid,
                    wav_path=str(wav_path),
                    force_single_channel=force_single,
                )
                src_path.unlink(missing_ok=True)

                # Spawn the watchdog before the big executor call. It will
                # mark the row failed (advisory) if any in-pipeline phase
                # exceeds 2× its budget. It does NOT release the semaphore.
                watchdog_task = asyncio.create_task(
                    self._phase_watchdog(jid, audio_duration_s)
                )

                await self._run_pipeline_inner(
                    jid,
                    wav_path,
                    force_single_channel=force_single,
                    request_mode=request_mode,
                    audio_duration_s=audio_duration_s,
                )

            except staging.StagingCancelled:
                logger.info("[%s] cancelled mid-decode by user", jid)
                current = self.store.get(jid)
                if current is None or current.status not in ("done", "failed"):
                    self.store.set_failed(jid, "cancelled by user")
                src_path.unlink(missing_ok=True)
            except asyncio.TimeoutError:
                # Already set_failed inside _phase().
                src_path.unlink(missing_ok=True)
            except Exception as exc:
                # Only call set_failed if the inner pipeline did NOT already
                # mark the job terminal. Otherwise we'd overwrite a more
                # precise pipeline failure.
                error_repr = repr(exc)
                current = self.store.get(jid)
                if current is None or current.status not in ("done", "failed"):
                    self.store.set_failed(jid, error_repr)
                logger.exception(
                    "[%s] File job failed: %s", jid, error_repr
                )
                src_path.unlink(missing_ok=True)
            finally:
                if watchdog_task is not None:
                    watchdog_task.cancel()
                self._active_job_id = None

    # ── phase + progress + watchdog helpers ──────────────────────────────────

    async def _phase(
        self,
        jid: str,
        name: str,
        budget_s: float | object,
        fn,
        *args,
    ):
        """Run a synchronous *fn* in the executor under an explicit phase
        marker and a real ``asyncio.wait_for`` timeout.

        Used for ffprobe + ffmpeg_decode (the seams that ARE wrappable).
        On timeout: sets the row failed and re-raises ``asyncio.TimeoutError``
        for the caller to catch.

        ``budget_s`` may be a scalar or a ``Callable[[float], float]`` — for
        ffprobe + ffmpeg_decode it's always scalar, but the same helper could
        be reused for in-pipeline phases in the future.
        """
        # Resolve callable budgets at call-time.
        if callable(budget_s):
            try:
                budget_resolved = float(budget_s(0.0))  # ffprobe/decode are scalar
            except Exception:  # noqa: BLE001
                budget_resolved = 600.0
        else:
            budget_resolved = float(budget_s)

        self.store.update_phase(jid, name)
        logger.info("[%s] phase_start name=%s", jid, name)
        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self._executor, fn, *args),
                timeout=budget_resolved,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] phase_timeout name=%s budget_s=%.1f",
                jid, name, budget_resolved,
            )
            self.store.set_failed(
                jid, f"Phase '{name}' exceeded timeout {budget_resolved:.0f}s"
            )
            raise
        logger.info(
            "[%s] phase_done name=%s duration_ms=%d",
            jid, name, int((time.monotonic() - t0) * 1000),
        )
        return result

    def _on_progress(
        self,
        jid: str,
        phase: str,
        idx: int,
        total: int,
    ) -> None:
        """Pipeline-side callback: persist phase change + chunk progress.

        Called from inside the executor thread (mlx-whisper's tqdm patch +
        wall-clock fallback). NPE-guarded: a concurrent ``store.delete``
        leaves nothing to update.
        """
        job = self.store.get(jid)
        if job is None:
            return
        if phase != job.phase:
            self.store.update_phase(jid, phase)
        if total > 0:
            self.store.update_chunk(jid, idx, total)

    async def _phase_watchdog(
        self,
        jid: str,
        audio_duration_s: float,
    ) -> None:
        """Mark the row failed if a phase exceeds 2× its budget.

        HONEST limitations:
        - Runs as a separate task; sleeps 5s between checks.
        - Reads ``phase`` + ``phase_started_at`` from the live row.
        - On overrun: calls ``set_failed`` with an "advisory" reason.
        - Does NOT abort the executor thread.
        - Does NOT release the semaphore — ``_run_source``'s
          ``async with self._semaphore`` is awaiting the executor and will
          only exit when the executor returns naturally. New submissions
          will continue to receive 429 with Retry-After until that happens;
          the client surfaces this as a "Previous transcription still
          finishing on server" banner so the user does not see mysterious
          errors.
        """
        while True:
            await asyncio.sleep(5)
            job = self.store.get(jid)
            if job is None or job.status in ("done", "failed"):
                return
            if not job.phase or not job.phase_started_at:
                continue
            budget = PHASE_BUDGETS.get(job.phase, 600)
            try:
                budget_s = float(
                    budget(audio_duration_s) if callable(budget) else budget
                )
            except Exception:  # noqa: BLE001
                budget_s = 600.0
            elapsed = time.time() - job.phase_started_at
            if elapsed > budget_s * 2:
                logger.error(
                    "[%s] phase_watchdog FIRING: phase=%s elapsed=%.0fs"
                    " budget=%.0fs (advisory; executor still running,"
                    " semaphore still held)",
                    jid, job.phase, elapsed, budget_s,
                )
                self.store.set_failed(
                    jid,
                    f"Phase '{job.phase}' watchdog timeout (advisory)",
                )
                return
