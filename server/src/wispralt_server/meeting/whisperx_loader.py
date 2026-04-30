"""
meeting/whisperx_loader.py — WhisperX + CrisperWhisper singleton loader.

Design decisions (from plan + v3 deltas):
- CTranslate2 has NO MPS backend; WhisperX MUST run on CPU.
  ``compute_type="int8"`` is used to fit within the memory budget.
- Models are NOT loaded at import time.  ``load()`` is called from the meeting
  executor thread on first meeting job, via ``pipeline._ensure_models_loaded``.
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

    Called from the meeting executor thread on first meeting job via
    ``pipeline._ensure_models_loaded()``. Thread-safe: whisperx.load_model +
    load_align_model are CPU-bound and safe to call off the event loop.
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


def reset() -> None:
    """Drop singletons so the next load() starts clean. Used on partial-load failure
    in pipeline._ensure_models_loaded(). Best-effort: drops Python references but
    C-level PyTorch/CTranslate2 handles may not free immediately."""
    global _model, _align_model, _align_metadata
    _model = None
    _align_model = None
    _align_metadata = None


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

    # WhisperX runs VAD before transcription. When VAD detects no speech, the
    # underlying transformers pipeline tries inputs[0] on an empty list and
    # raises IndexError. Treat that as "channel has no speech" and return an
    # empty result so the meeting pipeline produces a valid (empty-segments)
    # transcript instead of crashing the whole job.
    try:
        result: dict = _model.transcribe(audio_16k, batch_size=8)  # type: ignore[union-attr]
    except IndexError:
        logger.info("WhisperX VAD found no speech in channel; returning empty.")
        return {"segments": []}

    if not result.get("segments"):
        return {"segments": []}

    aligned: dict = whisperx.align(
        result["segments"],
        _align_model,
        _align_metadata,
        audio_16k,
        device=_DEVICE,
        return_char_alignments=False,
    )
    return aligned
