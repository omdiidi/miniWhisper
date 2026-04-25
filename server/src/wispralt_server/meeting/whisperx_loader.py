"""
meeting/whisperx_loader.py — WhisperX + CrisperWhisper singleton loader.

Design decisions (from plan + v3 deltas):
- CTranslate2 has NO MPS backend; WhisperX MUST run on CPU.
  ``compute_type="int8"`` is used to fit within the memory budget.
- Models are NOT loaded at import time.  ``load()`` is called once by the
  FastAPI lifespan (via ``pipeline.bootstrap_models``) after env validation.
- ``transcribe_channel`` is a blocking function; call it via
  ``asyncio.to_thread`` or from the dedicated meeting ThreadPoolExecutor.
"""

from __future__ import annotations

import logging

import numpy as np
import whisperx  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_MODEL_ID = "nyrahealth/faster_CrisperWhisper"
_DEVICE = "cpu"
_COMPUTE_TYPE = "int8"
_LANGUAGE = "en"

# Module-level singletons — None until ``load()`` is called.
_model: object | None = None
_align_model: object | None = None
_align_metadata: dict | None = None


def load() -> None:
    """Load CrisperWhisper and the English word-alignment model.

    Must be called exactly once, from the main thread (FastAPI lifespan),
    before any call to ``transcribe_channel``.
    """
    global _model, _align_model, _align_metadata

    logger.info("Loading WhisperX model %s on %s …", _MODEL_ID, _DEVICE)
    _model = whisperx.load_model(
        _MODEL_ID,
        device=_DEVICE,
        compute_type=_COMPUTE_TYPE,
        language=_LANGUAGE,
    )

    logger.info("Loading WhisperX alignment model (language=%s) …", _LANGUAGE)
    _align_model, _align_metadata = whisperx.load_align_model(
        language_code=_LANGUAGE,
        device=_DEVICE,
    )

    logger.info("WhisperX ready.")


def transcribe_channel(audio_16k: np.ndarray) -> dict:
    """Transcribe *audio_16k* and return a WhisperX aligned-result dict.

    The returned dict has the standard WhisperX structure::

        {
            "segments": [
                {
                    "start": float,
                    "end": float,
                    "text": str,
                    "words": [{"word": str, "start": float, "end": float, "score": float}, ...]
                },
                ...
            ]
        }

    Parameters
    ----------
    audio_16k:
        1-D float32 PCM array sampled at 16 kHz.

    Returns
    -------
    dict
        WhisperX aligned-result dict with per-word timestamps.

    Raises
    ------
    RuntimeError
        If ``load()`` has not been called before this function.
    """
    if _model is None or _align_model is None or _align_metadata is None:
        raise RuntimeError(
            "WhisperX models are not loaded. Call whisperx_loader.load() first."
        )

    # Transcribe; batch_size=8 is a reasonable default for CPU int8.
    result: dict = _model.transcribe(audio_16k, batch_size=8)  # type: ignore[union-attr]

    # Align to get per-word timestamps.
    aligned: dict = whisperx.align(
        result["segments"],
        _align_model,
        _align_metadata,
        audio_16k,
        device=_DEVICE,
        return_char_alignments=False,
    )
    return aligned
