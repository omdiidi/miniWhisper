"""
meeting/diarize.py — Pyannote 3.1 speaker diarization.

Key design decisions (from plan + v3 deltas):
- Pyannote DOES support MPS (unlike WhisperX/CTranslate2).  The pipeline is
  moved to MPS if available, else CPU.
- Pyannote crashes on audio shorter than ~2 s.  The ``diarize`` function
  guards against this and returns an empty DataFrame.
- Pyannote 3.3.2 returns an ``Annotation`` object, NOT a DataFrame.
  ``annotation_to_df`` converts it via ``itertracks``.
- ``use_auth_token`` is still the correct kwarg in pyannote 3.3.2
  (do NOT switch to ``token=``).
- The pipeline singleton is loaded lazily via ``load(hf_token)`` called from
  ``pipeline.bootstrap_models`` during the FastAPI lifespan.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import torch
from pyannote.audio import Pipeline  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_PIPELINE_ID = "pyannote/speaker-diarization-3.1"
_MIN_DURATION_S: float = 2.0

# Module-level singleton — None until ``load()`` is called.
_pipeline: Pipeline | None = None


def load(hf_token: str) -> None:
    """Load the Pyannote diarization pipeline and move it to MPS if available.

    Parameters
    ----------
    hf_token:
        HuggingFace access token.  Must have accepted the gated model terms at
        ``pyannote/speaker-diarization-3.1`` and ``pyannote/segmentation-3.0``.

    Notes
    -----
    ``use_auth_token`` is the correct kwarg in pyannote 3.3.2; do NOT use
    ``token=`` which is only valid in newer versions.
    """
    global _pipeline

    logger.info("Loading Pyannote pipeline %s …", _PIPELINE_ID)
    _pipeline = Pipeline.from_pretrained(
        _PIPELINE_ID,
        use_auth_token=hf_token,
    )

    if torch.backends.mps.is_available():
        device = torch.device("mps")
        logger.info("Moving Pyannote pipeline to MPS.")
    else:
        device = torch.device("cpu")
        logger.info("MPS not available; Pyannote pipeline on CPU.")

    _pipeline.to(device)
    logger.info("Pyannote ready.")


def annotation_to_df(annotation: object) -> pd.DataFrame:
    """Convert a Pyannote ``Annotation`` to a tidy DataFrame.

    Parameters
    ----------
    annotation:
        A ``pyannote.core.Annotation`` instance returned by the pipeline.

    Returns
    -------
    pd.DataFrame
        Columns: ``start`` (float), ``end`` (float), ``speaker`` (str).
        Empty DataFrame with the same columns if *annotation* has no tracks.
    """
    rows = [
        {"start": segment.start, "end": segment.end, "speaker": label}
        for segment, _, label in annotation.itertracks(yield_label=True)  # type: ignore[union-attr]
    ]
    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=["start", "end", "speaker"])


def diarize(
    audio_16k: np.ndarray,
    *,
    min_speakers: int = 1,
    max_speakers: int = 8,
) -> pd.DataFrame:
    """Run speaker diarization on *audio_16k*.

    Parameters
    ----------
    audio_16k:
        1-D float32 PCM array at 16 kHz (mono).
    min_speakers:
        Minimum number of speakers to hypothesize.
    max_speakers:
        Maximum number of speakers to hypothesize.

    Returns
    -------
    pd.DataFrame
        Columns: ``start``, ``end``, ``speaker``.  Empty DataFrame if the
        audio is shorter than 2 s (Pyannote crashes on very short clips) or
        if no speakers were detected.

    Raises
    ------
    RuntimeError
        If ``load()`` has not been called before this function.
    """
    if _pipeline is None:
        raise RuntimeError(
            "Pyannote pipeline is not loaded. Call diarize.load(hf_token) first."
        )

    duration_s = len(audio_16k) / 16_000.0
    if duration_s < _MIN_DURATION_S:
        logger.debug(
            "Audio is %.2f s (< %.1f s); skipping diarization.",
            duration_s,
            _MIN_DURATION_S,
        )
        return pd.DataFrame(columns=["start", "end", "speaker"])

    # Pyannote expects a dict with "waveform" (2-D tensor: channels × samples)
    # and "sample_rate".
    waveform = torch.from_numpy(audio_16k).unsqueeze(0)  # shape: (1, N)
    annotation = _pipeline(
        {"waveform": waveform, "sample_rate": 16_000},
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    df = annotation_to_df(annotation)

    # A3: Post-condition diagnostics — log warnings for unexpected results but
    # always continue; these are observability aids, not hard failures.
    if not df.empty:
        distinct_labels = df["speaker"].nunique()
        if distinct_labels > max_speakers + 2:
            logger.warning(
                "Pyannote returned %d distinct speakers (max_speakers=%d); "
                "diarization may be over-segmented.",
                distinct_labels,
                max_speakers,
            )
    else:
        if duration_s > 5.0:
            logger.warning(
                "Pyannote returned 0 speakers for %.1f s audio; "
                "check model/device setup.",
                duration_s,
            )

    return df
