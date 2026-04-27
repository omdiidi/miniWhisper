"""
meeting/deepfilter.py — Denoise stub.

DeepFilterNet was removed because it pins numpy<2.0, which conflicts with
parakeet-mlx (numpy>=2.2.5). The meeting pipeline now passes audio through
unchanged. Re-introduce a numpy-2-compatible denoiser later if needed.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def get_df() -> None:
    return None


def deepfilter(audio: np.ndarray, src_sr: int) -> np.ndarray:
    del src_sr
    return audio
