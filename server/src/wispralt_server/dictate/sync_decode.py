"""
dictate/sync_decode.py — Synchronous audio decode to 16 kHz mono float32 PCM.

Used by the OpenAI-compat /v1/audio/transcriptions route. Tries libsndfile first
(handles wav/flac/ogg/aiff), falls through to ffmpeg pipe for mp3/m4a/mp4/webm/aac/mpeg.

CRITICAL: this function is SYNCHRONOUS. Callers MUST invoke via
`await asyncio.to_thread(decode_to_pcm, audio_bytes)` from async routes,
otherwise the 60s subprocess timeout will block the entire FastAPI event loop.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile

import numpy as np

from wispralt_server import audio as _audio
from wispralt_server._errors import CorruptAudioError, DecodeTimeoutError, UnsupportedAudioError

TARGET_SR = 16_000
TIMEOUT_S = 60


def decode_to_pcm(audio_bytes: bytes) -> tuple[np.ndarray, float]:
    """Decode arbitrary audio bytes to (samples_16k_mono_float32, duration_s).

    BLOCKING — wrap in asyncio.to_thread from async callers.

    Strategy:
      1. libsndfile via audio.decode_wav_bytes (wav/flac/ogg/aiff).
         decode_wav_bytes returns (samples, sample_rate_int). We downmix to mono
         (if stereo) and resample to TARGET_SR explicitly.
      2. On libsndfile failure (CorruptAudioError), fall through to ffmpeg.

    Raises:
      CorruptAudioError — libsndfile decoded but produced unusable output.
      UnsupportedAudioError — ffmpeg can't sniff or decode the format.
      DecodeTimeoutError — ffmpeg exceeded TIMEOUT_S.
    """
    try:
        samples, sr = _audio.decode_wav_bytes(audio_bytes)
        if samples.ndim == 2:
            samples = samples.mean(axis=1)
        samples = _audio.safe_resample(samples, sr, TARGET_SR)
        samples = samples.astype(np.float32, copy=False)
        return samples, len(samples) / float(TARGET_SR)
    except CorruptAudioError:
        pass  # fall through to ffmpeg

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH — install via 'brew install ffmpeg'"
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.close()
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", tmp.name,
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", str(TARGET_SR),
            "-acodec", "pcm_f32le",
            "-f", "f32le",
            "pipe:1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise DecodeTimeoutError(
                f"ffmpeg decode exceeded {TIMEOUT_S}s"
            ) from exc
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise UnsupportedAudioError(f"ffmpeg decode failed: {tail}")
        # .copy() — np.frombuffer over bytes returns READ-ONLY; downstream resample/MLX needs writable
        samples = np.frombuffer(result.stdout, dtype=np.float32).copy()
        if samples.size == 0:
            raise UnsupportedAudioError("ffmpeg produced empty output")
        return samples, len(samples) / float(TARGET_SR)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
