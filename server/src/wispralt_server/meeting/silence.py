"""
meeting/silence.py — Frame-based RMS silence detection.

Pure functions; no I/O, no model calls.  Used by the meeting pipeline to
auto-detect "in-person mode" when channel 2 (system audio) carries no signal.
"""

from __future__ import annotations

import numpy as np


def is_silent_robust(
    audio: np.ndarray,
    sr: int,
    threshold: float = 0.002,
    frame_ms: int = 100,
    silent_fraction: float = 0.90,
) -> bool:
    """Return True if *audio* is predominantly silent.

    The check is frame-based rather than whole-clip to tolerate brief audio
    transients (system notification sounds, etc.) without mis-classifying a
    real remote-mode call as in-person.

    Algorithm
    ---------
    1. Split *audio* into non-overlapping frames of *frame_ms* milliseconds
       (``frame_size = sr * frame_ms / 1000`` samples).  Any trailing samples
       that don't fill a complete frame are included as a partial frame.
    2. Compute the root-mean-square (RMS) amplitude of each frame.
    3. Count the frames whose RMS is strictly below *threshold*.
    4. Return True iff ``silent_frames / total_frames >= silent_fraction``.

    Parameters
    ----------
    audio:
        1-D float32 PCM array.  Assumed to be at sample rate *sr*.
    sr:
        Sample rate in Hz (e.g. 16 000).
    threshold:
        Per-frame RMS below which a frame is considered silent.
        Default matches ``SILENCE_THRESHOLD`` in config (0.002).
    frame_ms:
        Frame duration in milliseconds.  Default 100ms.
    silent_fraction:
        Fraction of frames that must be silent to declare the clip silent.
        Default 0.90 (90 %).

    Returns
    -------
    bool
        True if the clip is considered silent (in-person mode).
    """
    if audio.size == 0:
        return True

    frame_size = max(1, int(sr * frame_ms / 1000))

    # Pad to a whole number of frames so we don't discard the trailing samples.
    remainder = audio.size % frame_size
    if remainder != 0:
        pad_length = frame_size - remainder
        audio = np.concatenate([audio, np.zeros(pad_length, dtype=audio.dtype)])

    # Reshape into (n_frames, frame_size) and compute RMS per row.
    frames = audio.reshape(-1, frame_size)
    rms_per_frame: np.ndarray = np.sqrt(np.mean(frames ** 2, axis=1))

    total_frames = len(rms_per_frame)
    silent_frames = int(np.sum(rms_per_frame < threshold))

    return (silent_frames / total_frames) >= silent_fraction
