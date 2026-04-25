"""
audio.py — Pure data-access helpers for PCM audio.

This module contains no business logic and no model calls; it is the
"repository layer" for audio I/O.  All errors are converted to typed
exceptions so callers can distinguish corrupt uploads from programming bugs.
"""

from __future__ import annotations

import io

import librosa
import numpy as np
import soundfile as sf

from wispralt_server._errors import CorruptAudioError

# Re-export so callers can import from either location without breakage.
__all__ = ["CorruptAudioError", "decode_wav_bytes", "split_channels", "resample"]


# ── data-access functions ─────────────────────────────────────────────────────


def decode_wav_bytes(b: bytes) -> tuple[np.ndarray, int]:
    """Decode raw audio bytes into a float32 NumPy array.

    Parameters
    ----------
    b:
        Raw bytes from an uploaded audio file (WAV, FLAC, …).

    Returns
    -------
    (audio, sample_rate)
        *audio* is a 1-D or 2-D float32 array (channels last).
        *sample_rate* is an integer in Hz.

    Raises
    ------
    CorruptAudioError
        If *soundfile* cannot decode the bytes.
    """
    try:
        audio, sr = sf.read(io.BytesIO(b), dtype="float32", always_2d=False)
    except sf.SoundFileError as exc:
        raise CorruptAudioError(f"Cannot decode audio: {exc}") from exc
    return audio, int(sr)


def split_channels(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split a mono or stereo array into (channel_0, channel_1).

    For a 2-channel array the two channels are returned.
    For a 1-channel / mono array channel_1 is a zero array of the same shape.

    Parameters
    ----------
    audio:
        Shape (N,) for mono or (N, 2) for stereo.

    Returns
    -------
    (ch0, ch1)
        Both arrays are 1-D with the same length.
    """
    if audio.ndim == 1:
        return audio, np.zeros_like(audio)
    if audio.ndim == 2 and audio.shape[1] >= 2:
        return audio[:, 0], audio[:, 1]
    if audio.ndim == 2 and audio.shape[1] == 1:
        ch = audio[:, 0]
        return ch, np.zeros_like(ch)
    raise ValueError(f"Unexpected audio shape: {audio.shape}")


def resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample *audio* from *src_sr* Hz to *dst_sr* Hz.

    Parameters
    ----------
    audio:
        1-D float32 PCM array.
    src_sr:
        Source sample rate in Hz.
    dst_sr:
        Target sample rate in Hz.

    Returns
    -------
    np.ndarray
        Resampled 1-D float32 array.
    """
    if src_sr == dst_sr:
        return audio
    return librosa.resample(audio, orig_sr=src_sr, target_sr=dst_sr)
