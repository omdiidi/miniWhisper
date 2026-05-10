"""
meeting/pipeline.py — Meeting transcription pipeline orchestration.

This is the service layer for the meeting feature.  It owns:
- Models load lazily on first ``transcribe_meeting()`` call via ``_ensure_models_loaded()``.
- ``transcribe_meeting(...)``     — blocking function run in the dedicated
  ThreadPoolExecutor by ``jobs/runner.py``.

Algorithm (pseudocode 5 from the plan):
1. Read the 2-channel WAV, split into ch1 (mic) and ch2 (system audio).
2. Detect "in-person mode": frame-based RMS silence check on ch2.
3. Denoise ch1 with DeepFilterNet.
4. Transcribe ch1 with WhisperX CrisperWhisper (CPU int8).
5a. In-person mode:
    - Diarise ch1 with Pyannote (MPS).
    - If diarization is empty → label all segments "Speaker 1".
    - Otherwise assign word-level speakers → relabel as "Speaker N".
5b. Remote mode:
    - Denoise ch2 and transcribe it.
    - Diarise ch2 (max 5 speakers).
    - Assign word-level speakers on ch2 (if not empty).
    - label_all(ch1, "You"), label_others(ch2), merge two channels.
6. Build the transcript dict (v3 locked schema) including ``speakers`` table.
7. Write outputs atomically (JSON + SRT + VTT + TXT).
8. Return the transcript dict (runner.py stores it in JobStore).

v3 schema reminder
------------------
speakers table keyed by ``speaker_raw``:
    {
      "mic":        {"display_name": "You",     "channel": 1},
      "SPEAKER_00": {"display_name": "Other",   "channel": 2},
    }
Each segment has both ``speaker`` (display name) and ``speaker_raw``.
"""

from __future__ import annotations

import datetime
import gc
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from wispralt_server.config import settings
from wispralt_server.meeting import deepfilter as _df_mod
from wispralt_server.meeting import diarize as _diarize_mod
from wispralt_server.meeting import install_compat_shims
from wispralt_server.meeting import mlx_whisper_loader as _mlx_mod
from wispralt_server.meeting.merge import (
    assign_speakers_segments,
    build_speakers_table,
    label_all,
    label_others,
    merge_two_channels,
    relabel_in_person,
)
from wispralt_server.meeting.output import write_outputs_atomic
from wispralt_server.meeting.silence import is_silent_robust

logger = logging.getLogger(__name__)

_MODEL_META = {
    "transcription": "mlx-community/whisper-large-v3-turbo",
    "diarization": "pyannote/speaker-diarization-3.1",
    "denoise": "deepfilternet-3",
}

# Ready flag — set to True by _ensure_models_loaded(); queried by /readyz/meeting.
_meeting_models_ready: bool = False
_load_lock = threading.RLock()
# In-flight flag for is_loading() observability. Cannot derive from RLock
# because RLock has no .locked() method (only Lock does). Set inside the
# critical section, cleared in the finally so observers see a stable value.
_loading_in_flight: bool = False
# Monotonic timestamp of the last successful or failed transcribe_meeting()
# return. Drives idle eviction. Initialised to the import time so a freshly
# started server with no meetings doesn't think it's been idle since the
# epoch and immediately try to evict (cold models can't be evicted, but the
# log spam would be confusing).
_last_meeting_finished_at: float = time.monotonic()


# ── lazy load ──────────────────────────────────────────────────────────────────


def _ensure_models_loaded() -> None:
    """Load WhisperX + Pyannote on first invocation; no-op thereafter.

    Called from transcribe_meeting() inside the meeting executor thread.
    Single-flight via threading.RLock — defensive; the runner's max_workers=1
    + Semaphore(1) already prevent concurrent calls today.

    Failure handling: if any sub-load raises, we call each loader's reset()
    helper to drop Python references, then gc.collect() as a hint. NOTE: this
    is best-effort — PyTorch and CTranslate2 hold C-level handles that may not
    free immediately. Traceback frames also retain locals until they unwind.
    RSS may stay elevated until the next allocation reuses the freed slabs.
    """
    global _meeting_models_ready, _loading_in_flight
    if _meeting_models_ready:
        return
    with _load_lock:
        if _meeting_models_ready:
            return
        _loading_in_flight = True
        try:
            logger.info("Lazy-loading meeting pipeline models (first meeting after start) …")
            install_compat_shims()
            try:
                _mlx_mod.load()
                _diarize_mod.load(settings.hf_token.get_secret_value())
                _df_mod.get_df()
            except Exception:
                _mlx_mod.reset()
                _diarize_mod.reset()
                gc.collect()
                raise
            _meeting_models_ready = True
            logger.info("Meeting pipeline models loaded and resident.")
        finally:
            _loading_in_flight = False


def is_ready() -> bool:
    """True if models have been loaded.  Drives /readyz/meeting models_warm."""
    return _meeting_models_ready


def is_loading() -> bool:
    """True iff a lazy load is currently in flight.

    Backed by an explicit _loading_in_flight bool (RLock has no .locked()
    method, only Lock does). Callers needing a coherent (warm, loading)
    snapshot must use state() — both reads happen between GIL release points
    so a racing flip from (loading=True, warm=False) to (loading=False, warm=True)
    can briefly observe (False, False).
    """
    return _loading_in_flight and not _meeting_models_ready


def state() -> tuple[bool, bool]:
    """Best-effort coherent (warm, loading) snapshot for observability endpoints.

    Acceptable for an observability endpoint — the next poll will reflect
    reality. Do NOT use this snapshot for control flow; use the per-call entry
    through transcribe_meeting() which is properly serialized.
    """
    warm = _meeting_models_ready
    loading = _loading_in_flight and not warm
    return warm, loading


def idle_seconds() -> float:
    """Seconds since the last meeting transcription completed (success or fail).

    Returns 0.0 if no meeting has ever run on this process. Drives /metrics
    meeting.idle_seconds and the idle-eviction background task.
    """
    if _last_meeting_finished_at == 0.0:
        return 0.0
    return time.monotonic() - _last_meeting_finished_at


def evict_if_idle(idle_threshold_s: float) -> bool:
    """If models are warm AND no meeting is in flight AND idle exceeds threshold,
    unload WhisperX + Pyannote and return True. Otherwise return False.

    Background task in main.py calls this every minute. Single-flight via the
    same _load_lock used by _ensure_models_loaded — eviction will skip rather
    than block if a meeting is currently loading or processing.

    Failure mode: same best-effort RAM reclaim caveat as the partial-load path
    (Python references dropped + gc.collect() hint; OS may hold slabs).
    """
    global _meeting_models_ready
    if not _meeting_models_ready:
        return False
    if idle_seconds() < idle_threshold_s:
        return False
    if not _load_lock.acquire(blocking=False):
        return False  # something is loading or another evict is running
    try:
        if not _meeting_models_ready:
            return False
        if _loading_in_flight:
            return False
        # Re-check idle under the lock — a meeting may have finished while we
        # were waiting and reset the timer.
        idle = idle_seconds()
        if idle < idle_threshold_s:
            return False
        logger.info(
            "Idle eviction: unloading meeting models after %.0fs idle "
            "(threshold %.0fs).",
            idle,
            idle_threshold_s,
        )
        # _mlx_mod.reset() is essentially a no-op for RAM reclaim because MLX
        # uses Apple's unified memory: dropping the Python ref does not free
        # the MLX backing pages eagerly. Pyannote + DeepFilterNet still hold
        # PyTorch tensors that benefit from the gc.collect() hint below.
        _mlx_mod.reset()
        _diarize_mod.reset()
        gc.collect()
        _meeting_models_ready = False
        return True
    finally:
        _load_lock.release()


# ── private helpers ────────────────────────────────────────────────────────────


def _load_channels(wav_path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Read a 2-channel WAV and return (ch1, ch2, sample_rate).

    Both channels are 1-D float32 arrays.  If the file is mono, ch2 is a
    zero array of the same length.

    Raises
    ------
    wispralt_server._errors.CorruptAudioError
        If soundfile cannot decode the file.
    """
    from wispralt_server._errors import CorruptAudioError

    # `sf.read` on a path can raise the same family as the bytes path
    # (LibsndfileError, OSError, EOFError, ValueError, MemoryError); map all
    # to CorruptAudioError so the route layer maps to 422.
    try:
        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, OSError, EOFError, ValueError, MemoryError) as exc:
        raise CorruptAudioError(f"Cannot decode WAV: {exc}") from exc
    if not isinstance(sr, int) or sr <= 0 or sr > 192_000:
        raise CorruptAudioError(f"Invalid sample rate: {sr}")

    # audio shape: (samples, channels)
    ch1 = audio[:, 0]
    if audio.shape[1] >= 2:
        ch2 = audio[:, 1]
    else:
        ch2 = np.zeros_like(ch1)

    return ch1, ch2, int(sr)


def _load_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV (any channel count) and collapse to a single mono stream.

    Used by the single-channel branch (custom transcriptions / mono uploads).
    Multi-channel inputs are averaged across channels.

    Raises
    ------
    wispralt_server._errors.CorruptAudioError
        If soundfile cannot decode the file.
    """
    from wispralt_server._errors import CorruptAudioError

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
    CorruptAudioError consistently across dictate and meeting paths.
    """
    from wispralt_server.audio import safe_resample

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


def transcribe_meeting(
    wav_path: Path,
    output_dir: Path,
    job_id: str,
    silence_threshold: float,
    *,
    force_single_channel: bool = False,
    request_mode: str = "meeting",
    progress_cb: Callable[[str, int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_setter_cb: Callable[[str], None] | None = None,
) -> dict:
    """Run the full meeting transcription pipeline and write output files.

    This is a **blocking** function; it must be called from the dedicated
    meeting ``ThreadPoolExecutor`` (via ``asyncio.to_thread``) managed by
    ``jobs/runner.py``, not from the FastAPI event loop.

    Parameters
    ----------
    wav_path:
        Path to the staged 2-channel 16 kHz WAV file.
    output_dir:
        Directory where ``{job_id}.{json,srt,vtt,txt}`` will be written.
    job_id:
        UUID job identifier.
    silence_threshold:
        Per-frame RMS threshold for in-person mode detection (from config).
    force_single_channel:
        If True, collapse to a single mono stream and skip diarization. Set by
        the caller (runner) when ``request_mode == ProcessingMode.FILE``.
    request_mode:
        Caller intent — ``"file"`` (no diarization) or ``"meeting"`` (full
        in-person/remote routing). Used for gating mode-specific behaviour
        (e.g. the mono-input warning is only meaningful for meetings).
    progress_cb:
        Optional ``(phase, idx, total)`` callable invoked from the mlx-whisper
        tqdm patch so the client can render chunk progress. Must be safe to
        call from a worker thread; exceptions are caught by the loader.
    cancel_cb:
        Optional zero-arg callable returning True if the user requested
        cancellation. Advisory only for in-pipeline phases — mlx-whisper
        cannot interrupt a decode mid-window.
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
    global _last_meeting_finished_at
    _ensure_models_loaded()
    logger.info("[%s] Starting meeting transcription pipeline …", job_id)
    try:
        return _transcribe_meeting_inner(
            wav_path,
            output_dir,
            job_id,
            silence_threshold,
            force_single_channel=force_single_channel,
            request_mode=request_mode,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
            phase_setter_cb=phase_setter_cb,
        )
    finally:
        # Stamp the idle timer on every exit (success OR failure) so the
        # background eviction task can correctly schedule unload.
        _last_meeting_finished_at = time.monotonic()


def _transcribe_meeting_inner(
    wav_path: Path,
    output_dir: Path,
    job_id: str,
    silence_threshold: float,
    *,
    force_single_channel: bool = False,
    request_mode: str = "meeting",
    progress_cb: Callable[[str, int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    phase_setter_cb: Callable[[str], None] | None = None,
) -> dict:
    """Inner pipeline body. Extracted from transcribe_meeting() so the outer
    function can wrap it in a try/finally that updates the idle timer.

    Phase 3 observability: at each in-pipeline phase boundary, calls
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

    # /transcribe/file mono branch — collapse to one stream and run the
    # pipeline with a single speaker dimension. Skips the in-person/remote
    # mode detection entirely (the source is conceptually a voice memo /
    # interview / lecture, not a 2-party meeting).
    if force_single_channel:
        # Phase: transcribe_load (covers WAV decode + resample + denoise +
        # any first-call MLX cold-start that happened during _ensure_models).
        _t = _phase_mark("transcribe_load")
        mono_raw, src_sr = _load_mono(wav_path)
        logger.debug(
            "[%s] Loaded mono WAV: sr=%d, duration=%.1fs",
            job_id, src_sr, len(mono_raw) / src_sr,
        )
        mono_16k = _resample_to_16k(mono_raw, src_sr)

        logger.debug("[%s] Denoising mono stream …", job_id)
        mono_clean = _df_mod.deepfilter(mono_16k, src_sr=16_000)
        _phase_mark("transcribe_load", t0=_t)

        # Phase: transcribe.
        _t = _phase_mark("transcribe")
        logger.debug("[%s] Transcribing mono stream …", job_id)
        # word_timestamps is False here: this branch is taken only when the
        # caller set force_single_channel=True (i.e. request_mode=FILE), and
        # file-mode never diarizes so we don't need word-level boundaries.
        # Skipping words also avoids a documented mlx-whisper memory growth
        # pattern on long inputs (single voice memos / lectures).
        mono_result = _mlx_mod.transcribe_channel(
            mono_clean,
            word_timestamps=(not force_single_channel),
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        _phase_mark("transcribe", t0=_t)

        # Phase: merge (single-channel: just label_all; no diarize).
        _t = _phase_mark("merge")
        # Default for single-channel: NO diarization. Typical inputs are
        # one-speaker (voice memo / dictation / lecture). Reuse the existing
        # `label_all` helper from merge.py for the single-speaker labelling.
        mono_segments = label_all(
            mono_result,
            display_name="Speaker 1",
            channel=None,
            raw_speakers=["mic"],
        )

        transcript = _build_transcript(
            job_id=job_id,
            mode="single",
            segments=mono_segments,
            audio_16k=mono_16k,
        )
        _phase_mark("merge", t0=_t)

        # Phase: output_write.
        _t = _phase_mark("output_write")
        logger.debug("[%s] Writing single-channel output files to %s …", job_id, output_dir)
        write_outputs_atomic(transcript, output_dir, job_id)
        _phase_mark("output_write", t0=_t)
        logger.info(
            "[%s] Pipeline complete: mode=single, segments=%d",
            job_id, len(mono_segments),
        )
        return transcript

    # ── Step 1: Load channels ─────────────────────────────────────────────────
    _t = _phase_mark("transcribe_load")
    ch1_raw, ch2_raw, src_sr = _load_channels(wav_path)
    logger.debug("[%s] Loaded WAV: sr=%d, duration=%.1fs", job_id, src_sr, len(ch1_raw) / src_sr)

    # Resample to 16 kHz (WhisperX / Pyannote requirement).
    ch1 = _resample_to_16k(ch1_raw, src_sr)
    ch2 = _resample_to_16k(ch2_raw, src_sr)

    # ── Step 1b: Detect mono input ───────────────────────────────────────────
    # I9: _load_channels returns a zero ch2 array when the WAV is mono.  Detect
    # this by checking the on-disk channel count and add a warnings entry so the
    # client/user can see why dual-channel features are absent.
    # Gated behind ``request_mode == "meeting"``: file-mode mono inputs are
    # normal (voice memos / lectures) and should not surface a warning.
    # ``force_single_channel`` is also checked for defensive symmetry — the
    # file-mode branch returns above before reaching this point, but guard
    # anyway so a future refactor doesn't accidentally warn.
    _mono_warnings: list[str] = []
    with sf.SoundFile(str(wav_path)) as _probe:
        _on_disk_channels = _probe.channels
    if (
        _on_disk_channels == 1
        and not force_single_channel
        and request_mode == "meeting"
    ):
        logger.warning("[%s] Input WAV is mono — dual-channel mode unavailable.", job_id)
        _mono_warnings.append("mono input — dual-channel mode unavailable")

    # ── Step 2: Detect in-person mode via ch2 silence ─────────────────────────
    in_person = is_silent_robust(
        ch2,
        sr=16_000,
        threshold=silence_threshold,
        frame_ms=100,
        silent_fraction=0.90,
    )
    mode = "in_person" if in_person else "remote"
    logger.info("[%s] Mode detected: %s", job_id, mode)

    # ── Step 3: Denoise and transcribe ch1 ───────────────────────────────────
    logger.debug("[%s] Denoising ch1 …", job_id)
    ch1_clean = _df_mod.deepfilter(ch1, src_sr=16_000)
    _phase_mark("transcribe_load", t0=_t)

    _t = _phase_mark("transcribe")
    logger.debug("[%s] Transcribing ch1 …", job_id)
    # word_timestamps=True for meeting mode (force_single_channel=False here):
    # downstream ``assign_speakers_segments`` uses the word-level start/end
    # values to split a transcribed segment at pyannote speaker boundaries.
    # Without words, two speakers talking inside one mlx-whisper window would
    # be glued into one segment with the largest-overlap speaker. ~20% perf
    # cost vs no-words; accepted trade for correct diarization.
    ch1_result = _mlx_mod.transcribe_channel(
        ch1_clean,
        word_timestamps=(not force_single_channel),
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
    )
    _phase_mark("transcribe", t0=_t)

    # ── Step 4a: In-person pipeline ───────────────────────────────────────────
    if in_person:
        _t = _phase_mark("diarize")
        logger.debug("[%s] Running in-person diarization on ch1 …", job_id)
        diar_df = _diarize_mod.diarize(ch1_clean, min_speakers=1, max_speakers=8)

        if diar_df.empty:
            logger.debug("[%s] Diarization empty; using single-speaker fallback.", job_id)
            segments = label_all(
                ch1_result,
                display_name="Speaker 1",
                channel=None,
                raw_speakers=["mic"],
            )
        else:
            logger.debug("[%s] Assigning speakers from diarization …", job_id)
            ch1_diar_segments = assign_speakers_segments(ch1_result, diar_df)
            segments = relabel_in_person(
                {"segments": ch1_diar_segments}, channel=None
            )
        _phase_mark("diarize", t0=_t)

    # ── Step 4b: Remote pipeline ──────────────────────────────────────────────
    else:
        _t = _phase_mark("transcribe")  # second-channel transcribe
        logger.debug("[%s] Denoising ch2 …", job_id)
        ch2_clean = _df_mod.deepfilter(ch2, src_sr=16_000)

        logger.debug("[%s] Transcribing ch2 …", job_id)
        ch2_result = _mlx_mod.transcribe_channel(
            ch2_clean,
            word_timestamps=(not force_single_channel),
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        _phase_mark("transcribe", t0=_t)

        _t = _phase_mark("diarize")
        logger.debug("[%s] Running diarization on ch2 …", job_id)
        diar_df = _diarize_mod.diarize(ch2_clean, min_speakers=1, max_speakers=5)

        if not diar_df.empty:
            ch2_diar_segments = assign_speakers_segments(ch2_result, diar_df)
            ch2_diar = {"segments": ch2_diar_segments}
        else:
            ch2_diar = ch2_result

        seg_a = label_all(
            ch1_result,
            display_name="You",
            channel=1,
            raw_speakers=["mic"],
        )
        seg_b = label_others(ch2_diar, base="Other", channel=2)
        segments = merge_two_channels(seg_a, seg_b)
        _phase_mark("diarize", t0=_t)

    # ── Step 5: Build transcript ──────────────────────────────────────────────
    _t = _phase_mark("merge")
    transcript = _build_transcript(
        job_id=job_id,
        mode=mode,
        segments=segments,
        audio_16k=ch1,  # use ch1 length for duration
    )
    # I9: Add warnings field for non-standard input conditions (e.g. mono WAV).
    if _mono_warnings:
        transcript["warnings"] = _mono_warnings
    _phase_mark("merge", t0=_t)

    # ── Step 6: Write output files ────────────────────────────────────────────
    _t = _phase_mark("output_write")
    logger.debug("[%s] Writing output files to %s …", job_id, output_dir)
    write_outputs_atomic(transcript, output_dir, job_id)
    _phase_mark("output_write", t0=_t)

    logger.info(
        "[%s] Pipeline complete: mode=%s, segments=%d",
        job_id,
        mode,
        len(segments),
    )
    return transcript
