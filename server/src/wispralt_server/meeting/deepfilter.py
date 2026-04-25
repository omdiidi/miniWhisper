"""
meeting/deepfilter.py — DeepFilterNet 3 noise suppression.

DeepFilterNet requires exactly 48 kHz input.  This module handles the
16 kHz ↔ 48 kHz resampling around the ``enhance`` call.

The ``(model, df_state, _)`` triple returned by ``init_df`` is cached as a
module-level singleton and initialised lazily on first call to ``get_df()``.
The lifespan can warm it up by calling ``get_df()`` at startup so the first
real meeting job doesn't pay the initialisation cost.
"""

from __future__ import annotations

import logging

import librosa
import numpy as np
import torch
from df import enhance, init_df  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_DF_SR: int = 48_000

# Module-level cache; populated on first call to get_df().
_df_state_cache: tuple[object, object, object] | None = None


def get_df() -> tuple[object, object, object]:
    """Return the cached (model, df_state, _) triple, initialising if needed.

    Thread-safety note: this function is called from the FastAPI lifespan
    (single thread) at startup, so the lazy init path races only during the
    bootstrap phase — which is acceptable.  After ``bootstrap_models`` returns,
    ``_df_state_cache`` is never None and no further writes occur.
    """
    global _df_state_cache
    if _df_state_cache is None:
        logger.info("Initialising DeepFilterNet …")
        _df_state_cache = init_df()
        logger.info("DeepFilterNet ready.")
    return _df_state_cache


def deepfilter(audio: np.ndarray, src_sr: int) -> np.ndarray:
    """Denoise *audio* using DeepFilterNet 3.

    Resamples to 48 kHz, applies ``enhance``, then resamples back to
    *src_sr*.

    Parameters
    ----------
    audio:
        1-D float32 PCM array.
    src_sr:
        Source (and output) sample rate in Hz.

    Returns
    -------
    np.ndarray
        Denoised 1-D float32 PCM array at *src_sr*.
    """
    model, df_state, _ = get_df()

    # Resample to 48 kHz (DeepFilterNet requirement).
    if src_sr != _DF_SR:
        audio_48k: np.ndarray = librosa.resample(audio, orig_sr=src_sr, target_sr=_DF_SR)
    else:
        audio_48k = audio

    # enhance() expects a (1, N) tensor; squeeze back to 1-D after.
    tensor = torch.from_numpy(audio_48k).unsqueeze(0)
    enhanced_tensor = enhance(model, df_state, tensor)  # type: ignore[arg-type]
    enhanced: np.ndarray = enhanced_tensor.squeeze(0).numpy()

    # Resample back to original rate.
    if src_sr != _DF_SR:
        return librosa.resample(enhanced, orig_sr=_DF_SR, target_sr=src_sr)
    return enhanced
