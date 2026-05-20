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
__all__ = [
    "CorruptAudioError",
    "decode_wav_bytes",
    "split_channels",
    "resample",
    "safe_resample",
    "wav_header_duration_ms",
]


# ── data-access functions ─────────────────────────────────────────────────────


def decode_wav_bytes(b: bytes) -> tuple[np.ndarray, int]:
    """Decode raw audio bytes into a float32 NumPy array.

    Returns ``(audio, sample_rate)``.  *audio* is a 1-D or 2-D float32 array
    (channels last); *sample_rate* is an integer in Hz.

    Raises ``CorruptAudioError`` for any decode failure — covers
    ``LibsndfileError`` (modern soundfile), plus ``OSError``/``EOFError``/
    ``ValueError`` (which soundfile raises for unsupported formats and
    truncated streams) and ``MemoryError`` (header claims more frames than
    the host can allocate).
    """
    try:
        audio, sr = sf.read(io.BytesIO(b), dtype="float32", always_2d=False)
    except (sf.LibsndfileError, OSError, EOFError, ValueError, MemoryError) as exc:
        raise CorruptAudioError(f"Cannot decode audio: {exc}") from exc
    if not isinstance(sr, int) or sr <= 0 or sr > 192_000:
        raise CorruptAudioError(f"Invalid sample rate: {sr}")
    return audio, int(sr)


def safe_resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample with corrupt-input failures mapped to ``CorruptAudioError``.

    `librosa.resample` raises ``librosa.util.exceptions.ParameterError``
    (a ``ValueError`` subclass) for degenerate sample rates and other bad
    input; we catch those here so the route layer sees one error type.
    """
    if src_sr == dst_sr:
        return audio
    try:
        return librosa.resample(audio, orig_sr=src_sr, target_sr=dst_sr)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise CorruptAudioError(f"Cannot resample audio: {exc}") from exc


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


def wav_header_duration_ms(audio_bytes: bytes) -> float:
    """Return WAV audio duration in milliseconds by reading the header only.

    Scans for the ``b"data"`` marker after the RIFF header — does NOT assume a
    fixed offset because ``sox`` and similar tools may insert ``LIST`` or
    ``bext`` chunks before the data chunk. Also locates the ``b"fmt "`` chunk
    earlier in the file to read sample rate, channel count, and bit depth.

    Duration is computed from the data chunk's declared byte size rather than
    the file length, so trailing chunks (``LIST``, ``id3 ``, ...) after the
    audio payload are ignored.

    Raises ``CorruptAudioError`` if either marker is missing, the format chunk
    is too short, or ``bytes_per_frame`` is zero.
    """
    if not isinstance(audio_bytes, (bytes, bytearray, memoryview)):
        raise CorruptAudioError("wav_header_duration_ms: input must be bytes-like")
    buf = bytes(audio_bytes)
    if len(buf) < 12 or buf[0:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise CorruptAudioError("wav_header_duration_ms: missing RIFF/WAVE header")

    # Locate the `fmt ` chunk. Search starts after the 12-byte RIFF/WAVE header.
    fmt_pos = buf.find(b"fmt ", 12)
    if fmt_pos < 0:
        raise CorruptAudioError("wav_header_duration_ms: missing 'fmt ' chunk")
    fmt_body_start = fmt_pos + 8  # skip 4-byte marker + 4-byte chunk size
    if len(buf) < fmt_body_start + 16:
        raise CorruptAudioError("wav_header_duration_ms: truncated 'fmt ' chunk")
    # WAVE fmt layout (PCM): audio_format(2) num_channels(2) sample_rate(4)
    # byte_rate(4) block_align(2) bits_per_sample(2)
    num_channels = int.from_bytes(buf[fmt_body_start + 2 : fmt_body_start + 4], "little")
    sample_rate = int.from_bytes(buf[fmt_body_start + 4 : fmt_body_start + 8], "little")
    bits_per_sample = int.from_bytes(
        buf[fmt_body_start + 14 : fmt_body_start + 16], "little"
    )

    # Locate the `data` chunk — may come after `LIST`, `bext`, etc.
    data_pos = buf.find(b"data", fmt_body_start)
    if data_pos < 0:
        raise CorruptAudioError("wav_header_duration_ms: missing 'data' chunk")
    if len(buf) < data_pos + 8:
        raise CorruptAudioError("wav_header_duration_ms: truncated 'data' chunk header")
    data_byte_count = int.from_bytes(buf[data_pos + 4 : data_pos + 8], "little")

    bytes_per_frame = num_channels * (bits_per_sample // 8)
    if bytes_per_frame == 0:
        raise CorruptAudioError(
            f"wav_header_duration_ms: zero bytes_per_frame"
            f" (channels={num_channels}, bits_per_sample={bits_per_sample})"
        )
    if sample_rate <= 0:
        raise CorruptAudioError(
            f"wav_header_duration_ms: invalid sample_rate={sample_rate}"
        )

    return (data_byte_count / bytes_per_frame / sample_rate) * 1000.0


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
