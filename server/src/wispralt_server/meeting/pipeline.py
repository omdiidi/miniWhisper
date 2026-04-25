"""
meeting/pipeline.py — Meeting transcription pipeline orchestration.

This is the service layer for the meeting feature.  It owns:
- ``bootstrap_models(hf_token)``  — called once by the FastAPI lifespan.
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
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
import whisperx  # type: ignore[import-untyped]

from wispralt_server.meeting import deepfilter as _df_mod
from wispralt_server.meeting import diarize as _diarize_mod
from wispralt_server.meeting import whisperx_loader as _wx_mod
from wispralt_server.meeting.merge import (
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
    "transcription": "nyrahealth/faster_CrisperWhisper",
    "diarization": "pyannote/speaker-diarization-3.1",
    "denoise": "deepfilternet-3",
}

# Ready flag — set to True by bootstrap_models(); queried by /readyz/meeting.
_meeting_models_ready: bool = False


# ── bootstrap ──────────────────────────────────────────────────────────────────


def bootstrap_models(hf_token: str) -> None:
    """Load all meeting pipeline models.

    Called once during the FastAPI lifespan (after env validation) so that
    models are warm before the first request arrives.

    Parameters
    ----------
    hf_token:
        HuggingFace token used to download the gated Pyannote model.
    """
    global _meeting_models_ready

    logger.info("Bootstrapping meeting pipeline models …")

    _wx_mod.load()
    _diarize_mod.load(hf_token)
    _df_mod.get_df()  # warm DeepFilterNet lazy init

    _meeting_models_ready = True
    logger.info("Meeting pipeline models ready.")


def is_ready() -> bool:
    """Return True if all meeting models have been loaded."""
    return _meeting_models_ready


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

    try:
        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, RuntimeError) as exc:
        # soundfile raises LibsndfileError since 0.12+; older versions raise RuntimeError.
        raise CorruptAudioError(f"Cannot decode WAV: {exc}") from exc

    # audio shape: (samples, channels)
    ch1 = audio[:, 0]
    if audio.shape[1] >= 2:
        ch2 = audio[:, 1]
    else:
        ch2 = np.zeros_like(ch1)

    return ch1, ch2, int(sr)


def _resample_to_16k(audio: np.ndarray, src_sr: int) -> np.ndarray:
    """Resample *audio* to 16 kHz if not already at 16 kHz."""
    import librosa

    if src_sr == 16_000:
        return audio
    return librosa.resample(audio, orig_sr=src_sr, target_sr=16_000)


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
        If models have not been loaded (bootstrap_models not called).
    """
    logger.info("[%s] Starting meeting transcription pipeline …", job_id)

    # ── Step 1: Load channels ─────────────────────────────────────────────────
    ch1_raw, ch2_raw, src_sr = _load_channels(wav_path)
    logger.debug("[%s] Loaded WAV: sr=%d, duration=%.1fs", job_id, src_sr, len(ch1_raw) / src_sr)

    # Resample to 16 kHz (WhisperX / Pyannote requirement).
    ch1 = _resample_to_16k(ch1_raw, src_sr)
    ch2 = _resample_to_16k(ch2_raw, src_sr)

    # ── Step 1b: Detect mono input ───────────────────────────────────────────
    # I9: _load_channels returns a zero ch2 array when the WAV is mono.  Detect
    # this by checking the on-disk channel count and add a warnings entry so the
    # client/user can see why dual-channel features are absent.
    _mono_warnings: list[str] = []
    with sf.SoundFile(str(wav_path)) as _probe:
        _on_disk_channels = _probe.channels
    if _on_disk_channels == 1:
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

    logger.debug("[%s] Transcribing ch1 …", job_id)
    ch1_result = _wx_mod.transcribe_channel(ch1_clean)

    # ── Step 4a: In-person pipeline ───────────────────────────────────────────
    if in_person:
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
            logger.debug("[%s] Assigning word-level speakers from diarization …", job_id)
            ch1_diar = whisperx.assign_word_speakers(diar_df, ch1_result)
            segments = relabel_in_person(ch1_diar, channel=None)

    # ── Step 4b: Remote pipeline ──────────────────────────────────────────────
    else:
        logger.debug("[%s] Denoising ch2 …", job_id)
        ch2_clean = _df_mod.deepfilter(ch2, src_sr=16_000)

        logger.debug("[%s] Transcribing ch2 …", job_id)
        ch2_result = _wx_mod.transcribe_channel(ch2_clean)

        logger.debug("[%s] Running diarization on ch2 …", job_id)
        diar_df = _diarize_mod.diarize(ch2_clean, min_speakers=1, max_speakers=5)

        if not diar_df.empty:
            ch2_diar = whisperx.assign_word_speakers(diar_df, ch2_result)
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

    # ── Step 5: Build transcript ──────────────────────────────────────────────
    transcript = _build_transcript(
        job_id=job_id,
        mode=mode,
        segments=segments,
        audio_16k=ch1,  # use ch1 length for duration
    )
    # I9: Add warnings field for non-standard input conditions (e.g. mono WAV).
    if _mono_warnings:
        transcript["warnings"] = _mono_warnings

    # ── Step 6: Write output files ────────────────────────────────────────────
    logger.debug("[%s] Writing output files to %s …", job_id, output_dir)
    write_outputs_atomic(transcript, output_dir, job_id)

    logger.info(
        "[%s] Pipeline complete: mode=%s, segments=%d",
        job_id,
        mode,
        len(segments),
    )
    return transcript
