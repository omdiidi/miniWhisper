"""
jobs/runner.py — In-process file transcription runner.

Design decisions (v3 deltas P4#3 + P5#1 + P5#11):
- One job at a time: admission (``_workers``, checked under ``_submit_lock``)
  rejects a second submission while a job is queued or running; the
  asyncio.Semaphore(1) only serialises execution of admitted workers.
- Dedicated ThreadPoolExecutor(max_workers=1): keeps file-job work isolated
  from the default asyncio thread pool used by other I/O tasks.
- OOM guard: reject a new job if less than 2 GiB RAM is available before
  starting.
- The staging WAV / source is cleaned up when the job ends (R1#2) — EXCEPT
  when the worker was interrupted (``CancelledError``, or the server is
  shutting down and the row is still ``running``): then the input is kept and
  ``store.requeue_interrupted`` puts the row back to ``pending`` with its
  attempt refunded, so a deploy never loses or spends a job.
- ``shutdown()`` (lifespan, before the DB goes away) cancels and awaits the
  workers, then shuts the executor down without waiting for its thread.
- reenqueue_pending(): called at startup after recover_orphans() to re-run any
  pending jobs whose source files survived the previous restart.

Phase deltas:
- ``_phase()`` wraps the seams that ARE wrappable (ffprobe + ffmpeg_decode):
  real ``asyncio.wait_for`` timeouts, real abort. The in-pipeline phases
  (transcribe / merge / output_write) cannot be interrupted because the
  executor thread is owned by mlx-whisper; per-phase budgets there are
  enforced by ``_phase_watchdog`` which is HONEST about its limitations: it
  marks the row failed for UI purposes but does NOT release the semaphore.
  New submissions still 429 until the executor returns naturally.
- ``cancel_cb`` is wired into ``staging.transcode_to_canonical_wav`` (Popen
  poll loop) so cancel-mid-decode works cleanly.

Cancel rule (atomic, see ``JobStore.set_running`` /
``JobStore.set_done_if_not_cancelled``):
- Cancel before start: ``set_running`` refuses a cancelled row, so the
  pipeline never runs and the job ends ``failed: cancelled by user``.
- Cancel mid-transcribe: Whisper finishes the whole file (one call), then
  ``set_done_if_not_cancelled`` refuses the row, the outputs are removed and
  the job ends ``failed: cancelled by user`` with ``transcript_text`` NULL.
- Exception path: outputs are removed (``write_outputs_atomic`` can leave
  earlier formats behind when a later write fails).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from .._errors import FileJobInProgressError
from ..config import settings
from ..files import pipeline as files_pipeline
from ..files.output import remove_job_outputs
from ..ops import staging
from .store import MAX_ATTEMPTS, JobStore

logger = logging.getLogger(__name__)

_2GiB = 2 * 1024**3

_CANCELLED = "cancelled by user"


# Per-phase wall budgets (seconds). Scalar entries are absolute; callable
# entries take audio duration in seconds and return a budget that scales with
# content length. The watchdog calls a budget by ``budget_s = budget(d) if
# callable(budget) else budget``.
PHASE_BUDGETS: dict[str, float | object] = {
    "ffprobe": 30,
    "ffmpeg_decode": 600,
    # Cold-start tax: first job pays the MLX load.
    "transcribe_load": 120,
    "transcribe": (lambda d: d * 4 + 120),
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
    "merge": "Finalizing transcript",
    "output_write": "Writing outputs",
}


class FileJobRunner:
    """Single-file-job-at-a-time runner backed by a dedicated thread executor.

    Lifecycle::

        runner = FileJobRunner(store, app.state)
        jid = await runner.submit_source_or_429(src_path)  # FileJobInProgressError if busy
        # ... poll store.get(jid).status ...

    At startup, after ``JobStore.recover_orphans()``::

        await runner.reenqueue_pending()
    """

    def __init__(self, store: JobStore, app_state: object) -> None:
        self.store = store
        # ``app.state``: ``shutting_down`` tells an interrupted job from a finished one.
        self.app_state = app_state
        # Dedicated single-thread pool — isolates heavy file-job I/O + CPU from
        # the default pool used by asyncio.to_thread for lighter tasks.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wispralt-files"
        )
        # Semaphore enforces at-most-one concurrent file job.
        self._semaphore = asyncio.Semaphore(1)
        self._active_job_id: str | None = None
        # C8: submit lock prevents TOCTOU race between the semaphore check,
        # store.create, and asyncio.create_task.  All three must be atomic.
        self._submit_lock = asyncio.Lock()
        # Worker tasks spawned but not yet finished (queued OR running). The
        # busy check reads this, not ``_semaphore.locked()``: a task created by
        # one submit has not acquired the semaphore yet when the next submit
        # runs, so the semaphore alone would admit a second job. The set also
        # holds strong references so the event loop cannot drop a task.
        self._workers: set[asyncio.Task[None]] = set()

    # ── public properties ──────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """True while a file job is queued or running (same signal as admission)."""
        return bool(self._workers)

    @property
    def active_job_id(self) -> str | None:
        """Job ID of the currently running job, or None."""
        return self._active_job_id

    # ── public methods ─────────────────────────────────────────────────────────

    async def submit_source_or_429(
        self,
        src_path: Path,
        *,
        user_id: int,
        client_version: str | None = None,
    ) -> str:
        """Create a job for a NOT-YET-TRANSCODED source and fire-and-forget it.

        Used by /transcribe/file: the source is staged in its original
        container; the worker (:meth:`_run_source`) runs ffprobe + ffmpeg
        before handing the canonical WAV to the pipeline.

        ``client_version`` + ``user_id`` are persisted on the original
        ``store.create`` so they survive worker crashes and restart-driven
        re-enqueues.

        Raises FileJobInProgressError (the route converts it to HTTP 429 with
        ``code`` in the body) if:
        - A job is already queued or running → ``file_job_busy``, OR
        - Available RAM is below 2 GiB (P5#11 OOM guard) → ``insufficient_memory``.
        """
        async with self._submit_lock:
            if self._workers:
                raise FileJobInProgressError(
                    "file job in progress: another file is currently being transcribed",
                    code="file_job_busy",
                )
            if psutil.virtual_memory().available < _2GiB:
                raise FileJobInProgressError(
                    "file job in progress: insufficient memory to start a new"
                    " file job; try again later",
                    code="insufficient_memory",
                )

            jid = self.store.create(
                src_path, user_id=user_id, client_app_version=client_version
            )
            self._spawn(jid, src_path)
            return jid

    async def reenqueue_pending(self) -> None:
        """Re-enqueue pending jobs whose source files survived the last restart.

        Called once at startup after ``JobStore.recover_orphans()`` returns its
        "requeue" list.  Each job is submitted as a new asyncio task; the
        semaphore ensures they run one at a time.

        Every pending row (including legacy ``.wav`` rows) goes through
        :meth:`_run_source`, which re-probes and transcodes it.

        C9: Jobs with attempts >= MAX_ATTEMPTS are skipped and immediately
        failed to prevent unbounded poison-pill re-enqueue loops.
        """
        rows = self.store.list_pending_jobs()
        if rows:
            logger.info(
                "Re-enqueueing %d pending job(s) from last run: %s",
                len(rows),
                [r[0] for r in rows],
            )
        for jid, src_path in rows:
            job = self.store.get(jid)
            if job is None:
                continue
            if job.attempts >= MAX_ATTEMPTS:
                logger.warning(
                    "[%s] Skipping re-enqueue: job has %d attempts (max %d). Marking failed.",
                    jid,
                    job.attempts,
                    MAX_ATTEMPTS,
                )
                self.store.set_failed(jid, "max retries exceeded")
                continue
            self._spawn(jid, src_path)

    async def shutdown(self) -> None:
        """Stop the workers (lifespan shutdown, before the DB and the lock are released).

        Cancelling a worker keeps its input and requeues its row (see
        ``_interrupted``). ``run_in_executor`` work cannot be cancelled, so
        the executor is shut down without waiting for its thread.
        """
        workers = list(self._workers)
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── private ───────────────────────────────────────────────────────────────

    def _interrupted(self, jid: str, cancelled: bool) -> bool:
        """True when the job stopped because the server is going down, not because it ended."""
        return cancelled or (
            bool(getattr(self.app_state, "shutting_down", False))
            and self.store.get_status(jid) == "running"
        )

    def _fail(self, jid: str, reason: str) -> None:
        """Fail *jid*; a requested cancel wins over any other reason (timeouts, watchdog, errors)."""
        self.store.set_failed(
            jid, _CANCELLED if self.store.check_cancel_requested(jid) else reason
        )

    def _spawn(self, jid: str, src_path: Path) -> None:
        """Start a worker task for *jid* and track it until it finishes."""
        task = asyncio.create_task(self._run_source(jid, src_path))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _run_pipeline_inner(
        self,
        jid: str,
        wav_path: Path,
    ) -> None:
        """Pipeline body without semaphore acquisition.

        The caller (:meth:`_run_source`) MUST hold ``self._semaphore`` and
        owns the pending → running transition. Threads ``progress_cb`` +
        ``cancel_cb`` to the pipeline so the client UI can render per-phase
        progress. Applies the atomic cancel rule on completion and removes
        outputs on every path that does not end ``done``.
        """
        output_dir = settings.outputs_dir
        self._active_job_id = jid
        cancelled = False
        try:
            self.store.update_phase(jid, "transcribe_load")
            logger.info("[%s] File transcription started.", jid)

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
                    files_pipeline.transcribe_file,
                    wav_path,
                    output_dir,
                    jid,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    phase_setter_cb=phase_setter_cb,
                ),
            )

            # Guard: the watchdog may have already set the row to failed
            # while the executor was finishing up. Don't flip a failed row
            # back to done, and don't leave its outputs behind.
            current = self.store.get(jid)
            if current is not None and current.status == "failed":
                logger.warning(
                    "[%s] executor returned post-failure (%s); discarding result",
                    jid, current.error,
                )
                remove_job_outputs(output_dir, jid)
                return

            mode: str = transcript.get("mode", "unknown")

            # Derive plain text from segments and persist on the row.
            # ``transcribe_file`` returns segments, NOT a top-level "text"
            # field (see files/pipeline.py:_build_transcript). Fallback: read
            # the pipeline's <output_dir>/<job_id>.txt file in case segments
            # are empty or shaped unexpectedly. NULL is the contract when
            # neither produces text (silence-only audio).
            segments = transcript.get("segments") or []
            transcript_text = " ".join(
                (seg.get("text") or "").strip()
                for seg in segments
                if isinstance(seg, dict)
            ).strip()
            if not transcript_text:
                txt_path = output_dir / f"{jid}.txt"
                if txt_path.exists():
                    try:
                        transcript_text = txt_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        logger.warning(
                            "[%s] transcript .txt fallback read failed: %s",
                            jid, exc,
                        )
                        transcript_text = ""

            # Atomic cancel rule: one UPDATE ... WHERE status='running' AND
            # cancel_requested=0. On False nothing may survive as done.
            if not self.store.set_done_if_not_cancelled(
                jid, mode, transcript_text=transcript_text or None
            ):
                remove_job_outputs(output_dir, jid)
                if self.store.check_cancel_requested(jid):
                    self.store.set_failed(jid, _CANCELLED)
                    logger.info("[%s] File job cancelled by user; result discarded.", jid)
                else:
                    logger.warning(
                        "[%s] File job no longer running at completion; result discarded.",
                        jid,
                    )
                return
            logger.info("[%s] File job done (mode=%s).", jid, mode)

        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            error_repr = repr(exc)
            current = self.store.get(jid)
            if current is None or current.status != "done":
                # write_outputs_atomic can leave earlier formats behind when a
                # later write fails; never leave partial outputs for a job
                # that did not finish.
                remove_job_outputs(output_dir, jid)
            if current is None or current.status not in ("done", "failed"):
                self._fail(jid, error_repr)
            logger.exception("[%s] File job failed: %s", jid, error_repr)

        finally:
            self._active_job_id = None
            # Clean up the staging WAV (R1#2) unless shutdown interrupted the
            # job: then it is the input the requeued row needs.
            if not self._interrupted(jid, cancelled):
                staging.cleanup(wav_path)

    async def _run_source(
        self,
        jid: str,
        src_path: Path,
    ) -> None:
        """Worker for /transcribe/file submissions.

        Sequence: ffprobe (channels) → ffprobe (duration) → ffmpeg transcode
        → pipeline. Status uses the existing ``running`` value (set via
        ``set_running``, the ONLY pending → running transition per attempt)
        — no new ``transcoding`` status (would require updating every status
        consumer).

        Phase markers via :meth:`_phase` make ffprobe + ffmpeg_decode
        independently observable AND independently abortable: each is its
        own ``run_in_executor`` call wrapped in ``asyncio.wait_for`` so a
        hung ffmpeg cannot eat the executor forever.

        The watchdog spawned via ``asyncio.create_task`` covers the
        in-pipeline phases (transcribe / merge / output_write).
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
            cancelled = False
            try:
                self._active_job_id = jid
                # Atomic: refuses a row whose cancel was requested before the
                # worker got here, so a cancelled job can never start.
                if not self.store.set_running(jid):
                    src_path.unlink(missing_ok=True)
                    if self.store.check_cancel_requested(jid):
                        self.store.set_failed(jid, _CANCELLED)
                        logger.info("[%s] File job cancelled before start.", jid)
                    else:
                        logger.warning(
                            "[%s] File job not pending at start; skipping.", jid
                        )
                    return
                self.store.update_phase(jid, "starting")
                logger.info("[%s] File job started: %s", jid, src_path)

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

                # Mono stays mono; anything else is staged as stereo and the
                # pipeline's _load_mono averages the channels (the only downmix).
                target_channels = 1 if channel_count == 1 else 2

                # Phase 2: ffmpeg_decode — REAL timeout, REAL abort, cancel-aware.
                cancel_cb = lambda: self.store.check_cancel_requested(jid)  # noqa: E731
                wav_path = await self._phase(
                    jid, "ffmpeg_decode", PHASE_BUDGETS["ffmpeg_decode"],
                    functools.partial(
                        staging.transcode_to_canonical_wav,
                        src_path,
                        target_channels=target_channels,
                        cancel_cb=cancel_cb,
                    ),
                )

                # Durability boundary: persist canonical wav_path BEFORE
                # deleting the source.
                self.store.update_after_transcode(jid, wav_path=wav_path)
                src_path.unlink(missing_ok=True)

                # A cancel that landed during the decode stops here, before
                # the file model is ever loaded.
                if self.store.check_cancel_requested(jid):
                    self.store.set_failed(jid, _CANCELLED)
                    staging.cleanup(wav_path)
                    logger.info("[%s] File job cancelled after decode.", jid)
                    return

                # Spawn the watchdog before the big executor call. It will
                # mark the row failed (advisory) if any in-pipeline phase
                # exceeds 2× its budget. It does NOT release the semaphore.
                watchdog_task = asyncio.create_task(
                    self._phase_watchdog(jid, audio_duration_s)
                )

                await self._run_pipeline_inner(jid, wav_path)

            except asyncio.CancelledError:
                # Shutdown: keep the source (and any canonical WAV); the row
                # is requeued below.
                cancelled = True
                raise
            except staging.StagingCancelled:
                logger.info("[%s] cancelled mid-decode by user", jid)
                current = self.store.get(jid)
                if current is None or current.status not in ("done", "failed"):
                    self.store.set_failed(jid, _CANCELLED)
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
                    self._fail(jid, error_repr)
                logger.exception(
                    "[%s] File job failed: %s", jid, error_repr
                )
                src_path.unlink(missing_ok=True)
            finally:
                if watchdog_task is not None:
                    watchdog_task.cancel()
                self._active_job_id = None
                if self._interrupted(jid, cancelled):
                    self.store.requeue_interrupted(jid)
                    logger.info("[%s] Interrupted by shutdown; input kept, job requeued.", jid)

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
            self._fail(jid, f"Phase '{name}' exceeded timeout {budget_resolved:.0f}s")
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
        wall-clock fallback). NPE-guarded: a row that is gone (retention)
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
                self._fail(jid, f"Phase '{job.phase}' watchdog timeout (advisory)")
                return
