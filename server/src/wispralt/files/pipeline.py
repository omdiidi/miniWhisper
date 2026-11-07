"""
files/pipeline.py — Uploaded-file transcription pipeline orchestration.

This is the service layer for file jobs.  It owns:
- Lazy model load on the first ``transcribe_file()`` call via ``_ensure_models_loaded()``.
- ``transcribe_file(...)`` — blocking function run in the dedicated
  ThreadPoolExecutor by ``jobs/runner.py``.
- ``evict_if_idle(...)`` — idle unload of mlx-whisper, driven by the
  background eviction loop.

Algorithm:
1. Read the canonical WAV (any channel count) and average to one mono stream.
2. Resample to 16 kHz.
3. Transcribe with mlx-whisper turbo (Apple Silicon GPU), no word timestamps.
4. Label every segment "Speaker 1" and build the transcript dict (v3 schema,
   ``mode="single"``, ``model`` lists the ASR model only).
5. Write outputs atomically (JSON + SRT + VTT + TXT).
6. Return the transcript dict (runner.py stores its text in JobStore).
"""

from __future__ import annotations

import datetime
import gc
import importlib.metadata as _meta
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import mlx.core as mx
import numpy as np
import soundfile as sf

from wispralt import observability
from wispralt.config import settings
from wispralt.files import mlx_whisper_loader as _mlx_mod
from wispralt.files.output import write_outputs_atomic
from wispralt.files.segments import build_speakers_table, label_all

logger = logging.getLogger(__name__)

_MODEL_META = {
    "transcription": "mlx-community/whisper-large-v3-turbo",
}

# Ready flag — set to True by _ensure_models_loaded(); read via is_ready()/state().
_models_ready: bool = False
_load_lock = threading.RLock()
# In-flight flag for is_loading() observability. Cannot derive from RLock
# because RLock has no .locked() method (only Lock does). Set inside the
# critical section, cleared in the finally so observers see a stable value.
_loading_in_flight: bool = False
# Monotonic timestamp of the last successful or failed transcribe_file()
# return. Drives idle eviction. Initialised to the import time so a freshly
# started server with no file jobs doesn't think it's been idle since the
# epoch and immediately try to evict (cold models can't be evicted, but the
# log spam would be confusing).
_last_job_finished_at: float = time.monotonic()


# ── lazy load ──────────────────────────────────────────────────────────────────


def _ensure_models_loaded() -> None:
    """Load mlx-whisper on first invocation; no-op thereafter.

    Called from transcribe_file() inside the file-job executor thread.
    Single-flight via threading.RLock — defensive; the runner's max_workers=1
    + Semaphore(1) already prevent concurrent calls today.

    Failure handling: if the load raises, we call the loader's reset()
    helper to drop Python references, then gc.collect() as a hint. NOTE: this
    is best-effort — traceback frames retain locals until they unwind, so RSS
    may stay elevated until the next allocation reuses the freed slabs.
    """
    global _models_ready, _loading_in_flight
    if _models_ready:
        return
    with _load_lock:
        if _models_ready:
            return
        _loading_in_flight = True
        try:
            logger.info("Lazy-loading file transcription model (first file job after start) …")
            try:
                _mlx_mod.load()
            except Exception:
                _mlx_mod.reset()
                gc.collect()
                raise
            _models_ready = True
            logger.info("File transcription model loaded and resident.")
        finally:
            _loading_in_flight = False


def is_ready() -> bool:
    """True if the file transcription model has been loaded."""
    return _models_ready


def is_loading() -> bool:
    """True iff a lazy load is currently in flight.

    Backed by an explicit _loading_in_flight bool (RLock has no .locked()
    method, only Lock does). Callers needing a coherent (warm, loading)
    snapshot must use state() — both reads happen between GIL release points
    so a racing flip from (loading=True, warm=False) to (loading=False, warm=True)
    can briefly observe (False, False).
    """
    return _loading_in_flight and not _models_ready


def state() -> tuple[bool, bool]:
    """Best-effort coherent (warm, loading) snapshot for observability endpoints.

    Acceptable for an observability endpoint — the next poll will reflect
    reality. Do NOT use this snapshot for control flow; use the per-call entry
    through transcribe_file() which is properly serialized.
    """
    warm = _models_ready
    loading = _loading_in_flight and not warm
    return warm, loading


def idle_seconds() -> float:
    """Seconds since the last file transcription completed (success or fail).

    Returns 0.0 if the idle timer was never stamped. Drives /admin/metrics
    meeting.idle_seconds and the idle-eviction background task.
    """
    if _last_job_finished_at == 0.0:
        return 0.0
    return time.monotonic() - _last_job_finished_at


def evict_if_idle(idle_threshold_s: float) -> bool:
    """If the model is warm AND no file job is in flight AND idle exceeds
    threshold, unload mlx-whisper and return True. Otherwise return False.

    Background task in main.py calls this every minute. Single-flight via the
    same _load_lock used by _ensure_models_loaded — eviction will skip rather
    than block if a file job is currently loading or processing.

    Failure mode: same best-effort RAM reclaim caveat as the partial-load path
    (Python references dropped + gc.collect() hint; OS may hold slabs).
    """
    global _models_ready
    if not _models_ready:
        return False
    if idle_seconds() < idle_threshold_s:
        return False
    if not _load_lock.acquire(blocking=False):
        return False  # something is loading or another evict is running
    # Locals captured inside the lock; logging + self-check fire AFTER the
    # lock releases so a misconfigured logging handler can't hold _load_lock
    # longer than necessary.
    did_evict = False
    pre_active_mb = 0
    post_active_mb = 0
    post_cache_mb_after = 0
    eviction_log_args: tuple | None = None  # (idle, threshold)
    try:
        if not _models_ready:
            return False
        if _loading_in_flight:
            return False
        # Re-check idle under the lock — a file job may have finished while we
        # were waiting and reset the timer.
        idle = idle_seconds()
        if idle < idle_threshold_s:
            return False
        # Save logger args for outside-lock emission (unified into the single
        # post-lock info log that also reports the memory delta).
        eviction_log_args = (idle, idle_threshold_s)
        pre_active_mb = observability.mlx_active_mb()
        # _mlx_mod.reset() actively clears mlx_whisper.transcribe.ModelHolder
        # (fixed in ca49731 / 2026-05-19 postmortem) — it is NOT a no-op.
        # gc.collect() is a hint so the dropped references are reclaimed now.
        _mlx_mod.reset()
        gc.collect()
        # Actually return MLX's unified-memory pages to the OS. _mlx_mod.reset()
        # above only drops the Python ref; Apple unified memory keeps the
        # backing pool cached unless we explicitly clear. Without this call,
        # idle RSS stays at 6.5-9 GB after a 105m job. With it, ~3 GB.
        # Note: mx.clear_cache() and mx.reset_peak_memory() are the modern
        # API (mx.metal.* are deprecated and emit a warning).
        try:
            mx.clear_cache()
            mx.reset_peak_memory()
        except Exception:  # noqa: BLE001 — best-effort; clear_cache is non-critical
            logger.debug("mx.clear_cache() failed during eviction", exc_info=True)
        post_active_mb = observability.mlx_active_mb()
        post_cache_mb_after = observability.mlx_cache_mb()
        _models_ready = False
        did_evict = True
        return True
    finally:
        _load_lock.release()
        # POST-LOCK: emit the eviction info log + self-check critical log
        # outside the lock so misconfigured logging handlers can't hold the
        # load lock longer than necessary.
        if did_evict and eviction_log_args is not None:
            _idle, _threshold = eviction_log_args
            logger.info(
                "Idle eviction: unloaded file transcription model after %.0fs idle "
                "(threshold %.0fs). Memory delta: pre_active=%d MB "
                "post_active=%d MB delta=%d MB post_cache=%d MB",
                _idle, _threshold, pre_active_mb, post_active_mb,
                pre_active_mb - post_active_mb, post_cache_mb_after,
            )
            # Self-check: invariant verification.
            delta_mb = pre_active_mb - post_active_mb
            if (pre_active_mb > settings.meeting_eviction_pre_active_gate_mb
                    and delta_mb < settings.meeting_eviction_delta_floor_mb):
                observability.eviction_failures_total.increment()
                try:
                    mlx_whisper_version = _meta.version("mlx-whisper")
                except Exception:
                    mlx_whisper_version = "?"
                logger.critical(
                    "Eviction invariant failed (pid=%d): pre_active=%d MB "
                    "post_active=%d MB delta=%d MB (floor=%d) post_cache=%d MB "
                    "mlx_whisper_version=%s. A library cache is likely "
                    "surviving the release path. See "
                    "docs/TROUBLESHOOTING.md section 'Trust invariants, "
                    "not flags'.",
                    os.getpid(), pre_active_mb, post_active_mb, delta_mb,
                    settings.meeting_eviction_delta_floor_mb,
                    post_cache_mb_after, mlx_whisper_version,
                )


# ── private helpers ────────────────────────────────────────────────────────────


def _load_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV (any channel count) and collapse to a single mono stream.

    Multi-channel inputs (stereo uploads stay stereo through staging) are
    averaged across channels here; this is the only downmix.

    Raises
    ------
    wispralt._errors.CorruptAudioError
        If soundfile cannot decode the file.
    """
    from wispralt._errors import CorruptAudioError

    try:
        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, OSError, EOFError, ValueError, MemoryError) as exc:
        raise CorruptAudioError(f"Cannot decode WAV: {exc}") from exc
    if not isinstance(sr, int) or sr <= 0 or sr > 192_000:
        raise CorruptAudioError(f"Invalid sample rate: {sr}")

    if audio.shape[1] > 1:
        mono = audio.mean(axis=1)
    else:
        mono = audio[:, 0]
    return mono, int(sr)


def _resample_to_16k(audio: np.ndarray, src_sr: int) -> np.ndarray:
    """Resample *audio* to 16 kHz if not already at 16 kHz.

    Delegates to ``audio.safe_resample`` so librosa errors map to
    CorruptAudioError consistently across dictate and file paths.
    """
    from wispralt.audio import safe_resample

    return safe_resample(audio, src_sr, 16_000)


def _build_transcript(
    job_id: str,
    mode: str,
    segments: list[dict],
    audio_16k: np.ndarray,
) -> dict:
    """Assemble the locked v3 transcript dict."""
    speakers_table = build_speakers_table(segments)

    return {
        "job_id": job_id,
        "mode": mode,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "duration_s": round(len(audio_16k) / 16_000.0, 3),
        "language": "en",
        "model": _MODEL_META,
        "segments": segments,
        "speakers": speakers_table,
    }

# ── public pipeline function ───────────────────────────────────────────────────


def transcribe_file(
    wav_path: Path,
    output_dir: Path,
    job_id: str,
    *,
    progress_cb: Callable[[str, int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_setter_cb: Callable[[str], None] | None = None,
) -> dict:
    """Run the file transcription pipeline and write output files.

    This is a **blocking** function; it must be called from the dedicated
    file-job ``ThreadPoolExecutor`` (via ``run_in_executor``) managed by
    ``jobs/runner.py``, not from the FastAPI event loop.

    Parameters
    ----------
    wav_path:
        Path to the staged canonical WAV (mono or stereo).
    output_dir:
        Directory where ``{job_id}.{json,srt,vtt,txt}`` will be written.
    job_id:
        UUID job identifier.
    progress_cb:
        Optional ``(phase, idx, total)`` callable invoked from the mlx-whisper
        tqdm patch so the client can render chunk progress. Must be safe to
        call from a worker thread; exceptions are caught by the loader.
    cancel_cb:
        Optional zero-arg callable returning True if the user requested
        cancellation. Advisory only — mlx-whisper cannot interrupt a decode
        mid-window, so a cancelled job finishes this call before the runner
        discards the result.
    phase_setter_cb:
        Optional one-arg callable invoked at each in-pipeline phase boundary
        with the phase name. The runner wires this to
        ``store.update_phase`` so the watchdog's elapsed calc resets per
        phase. Pipeline cannot import the store directly without a cycle.

    Returns
    -------
    dict
        Full transcript dict (v3 locked schema).

    Raises
    ------
    CorruptAudioError
        If the WAV cannot be decoded.
    DiskFullError
        If output writes fail with ENOSPC.
    RuntimeError
        If model load fails inside _ensure_models_loaded() on first call.
    """
    global _last_job_finished_at
    _ensure_models_loaded()
    logger.info("[%s] Starting file transcription pipeline …", job_id)
    try:
        return _transcribe_file_inner(
            wav_path,
            output_dir,
            job_id,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
            phase_setter_cb=phase_setter_cb,
        )
    finally:
        # Stamp the idle timer on every exit (success OR failure) so the
        # background eviction task can correctly schedule unload.
        _last_job_finished_at = time.monotonic()
        # Reclaim per-job MLX activation cache (separate from the model
        # weights, which evict_if_idle handles). Without this, peak RSS
        # compounds across back-to-back jobs.
        try:
            mx.clear_cache()
        except Exception:  # noqa: BLE001
            logger.debug("post-job mx.clear_cache() failed", exc_info=True)


def _transcribe_file_inner(
    wav_path: Path,
    output_dir: Path,
    job_id: str,
    *,
    progress_cb: Callable[[str, int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_setter_cb: Callable[[str], None] | None = None,
) -> dict:
    """Inner pipeline body. Extracted from transcribe_file() so the outer
    function can wrap it in a try/finally that updates the idle timer.

    Phase observability: at each in-pipeline phase boundary, calls
    ``phase_setter_cb(name)`` (a closure provided by the runner that invokes
    ``store.update_phase``) AND emits structured log lines
    ``[{jid}] phase_start name=X`` / ``phase_done name=X duration_ms=N``.
    The pipeline cannot import ``store`` directly without circular imports —
    the closure is the deliberate seam.
    """
    def _phase_mark(name: str, t0: float | None = None) -> float:
        """Emit phase_start/done log + invoke phase_setter_cb. Returns
        ``time.monotonic()`` so the caller can chain start → done in one
        local var. If *t0* is provided, this is a phase_done call."""
        if t0 is None:
            logger.info("[%s] phase_start name=%s", job_id, name)
            if phase_setter_cb is not None:
                try:
                    phase_setter_cb(name)  # type: ignore[misc]
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[%s] phase_setter_cb raised on %s; continuing",
                        job_id, name,
                    )
            return time.monotonic()
        logger.info(
            "[%s] phase_done name=%s duration_ms=%d",
            job_id, name, int((time.monotonic() - t0) * 1000),
        )
        return time.monotonic()


    # Phase: transcribe_load (covers WAV decode + resample + any first-call
    # MLX cold-start that happened during _ensure_models_loaded).
    _t = _phase_mark("transcribe_load")
    mono_raw, src_sr = _load_mono(wav_path)
    logger.debug(
        "[%s] Loaded WAV: sr=%d, duration=%.1fs",
        job_id, src_sr, len(mono_raw) / src_sr,
    )
    mono_16k = _resample_to_16k(mono_raw, src_sr)
    _phase_mark("transcribe_load", t0=_t)

    # Phase: transcribe. word_timestamps=False: file jobs never need
    # word-level boundaries, and skipping words avoids a documented
    # mlx-whisper memory growth pattern on long inputs.
    _t = _phase_mark("transcribe")
    mono_result = _mlx_mod.transcribe_channel(
        mono_16k,
        word_timestamps=False,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )
    _phase_mark("transcribe", t0=_t)

    # Phase: merge (single speaker label for the one mono stream).
    _t = _phase_mark("merge")
    segments = label_all(
        mono_result,
        display_name="Speaker 1",
        channel=None,
        raw_speakers=["mic"],
    )
    transcript = _build_transcript(
        job_id=job_id,
        mode="single",
        segments=segments,
        audio_16k=mono_16k,
    )
    _phase_mark("merge", t0=_t)

    # Phase: output_write.
    _t = _phase_mark("output_write")
    logger.debug("[%s] Writing output files to %s …", job_id, output_dir)
    write_outputs_atomic(transcript, output_dir, job_id)
    _phase_mark("output_write", t0=_t)
    logger.info(
        "[%s] Pipeline complete: mode=single, segments=%d",
        job_id, len(segments),
    )
    return transcript
