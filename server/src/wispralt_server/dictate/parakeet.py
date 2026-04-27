"""
dictate/parakeet.py — Parakeet TDT 0.6B v2 (MLX) service for dictation.

Key design decisions (from plan + v3 deltas):
- MLX inference is NOT thread-safe per model instance; we serialise via a
  single-thread ThreadPoolExecutor.
- ``model.generate`` may return either a Hypothesis object (with a ``.text``
  attribute) OR a list of AlignedToken objects (each with ``.text``).  Both
  shapes are handled by ``_extract_text``.
- First inference triggers Metal kernel JIT (300ms–2s); ``load()`` runs a
  warmup pass so the first real request isn't penalised.
- Recent inference durations (in ms) are tracked in a bounded deque for the
  /metrics endpoint.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import mlx.core as mx
import numpy as np

from parakeet_mlx import from_pretrained, DecodingConfig  # type: ignore[import-untyped]
from parakeet_mlx.audio import get_logmel  # type: ignore[import-untyped]

from ..audio import decode_wav_bytes, safe_resample
from .._errors import CorruptAudioError

logger = logging.getLogger(__name__)

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
TARGET_SR = 16_000
# Minimum samples required for a meaningful transcription (100ms of audio at 16kHz)
MIN_SAMPLES = 1_600
# Hard cap on post-resample length: 60s at 16kHz. Dictation is a hold-FN gesture;
# anything longer is either pathological encoding (ulaw amplification) or a
# meeting upload sent to the wrong endpoint. Prevents single-thread executor
# starvation by an attacker uploading a 1KB body that decodes to many minutes.
MAX_SAMPLES = TARGET_SR * 60


class ParakeetService:
    """Warm-loaded Parakeet transcription service.

    Lifecycle::

        service = ParakeetService()
        service.load()            # called once in FastAPI lifespan
        text, ms = await service.transcribe(wav_bytes)
    """

    def __init__(self) -> None:
        self.model: object | None = None
        self.ready: bool = False
        # Single-thread executor — MLX model is NOT thread-safe
        self._exec: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet")
        # Rolling window of recent inference durations (milliseconds), maxlen=100 for percentiles
        self.recent_durations: collections.deque[float] = collections.deque(maxlen=100)

    def load(self) -> None:
        """Load model weights and run a warmup pass to trigger Metal JIT compilation.

        Must be called from the main thread (or any single thread) before the
        first ``transcribe`` call.
        """
        logger.info("Loading Parakeet model %s …", MODEL_ID)
        self.model = from_pretrained(MODEL_ID, dtype=mx.bfloat16)

        # Warmup — forces Metal kernel compilation so the first real request
        # is not penalised by JIT overhead (300ms–2s on first call).
        logger.info("Running Parakeet warmup pass …")
        dummy = mx.zeros((TARGET_SR // 2,), dtype=mx.float32)
        mel = get_logmel(dummy, self.model.preprocessor_config)  # type: ignore[union-attr]
        result = self.model.generate(mel, decoding_config=DecodingConfig())  # type: ignore[union-attr]
        mx.eval(result)

        self.ready = True
        logger.info("Parakeet ready.")

    # ── private helpers ───────────────────────────────────────────────────────

    def _extract_text(self, result: object) -> str:
        """Defensively extract text from whatever ``model.generate`` returns.

        ``parakeet-mlx`` may return:
        - A Hypothesis object with a ``.text`` attribute, OR
        - A list of AlignedToken objects each with a ``.text`` attribute.

        Both shapes are valid; handle both.
        """
        if hasattr(result, "text"):
            return str(result.text).strip()  # type: ignore[union-attr]
        if isinstance(result, list) and result and hasattr(result[0], "text"):
            return "".join(str(t.text) for t in result).strip()
        logger.warning("Unexpected Parakeet result type: %s", type(result))
        return ""

    def _sync_transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
        """Blocking inference — runs inside the single-thread executor.

        Returns
        -------
        (text, duration_ms)
        """
        t0 = time.perf_counter()

        # Decode + resample at the audio.py boundary — every decode/resample
        # failure becomes CorruptAudioError so the route layer maps it to 422.
        audio_np, sr = decode_wav_bytes(audio_bytes)

        # Flatten stereo to mono
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1)

        # Resample to 16 kHz; safe_resample also maps librosa errors to 422.
        audio_np = safe_resample(audio_np, sr, TARGET_SR)

        # Reject decode-amplification: cap post-resample length at MAX_DURATION_S
        # to prevent ulaw/alaw or pathological-sr uploads decoding into multi-minute
        # arrays that block the single-thread executor.
        if len(audio_np) > MAX_SAMPLES:
            raise CorruptAudioError(
                f"Decoded audio too long: {len(audio_np) / TARGET_SR:.1f}s "
                f"(max {MAX_SAMPLES / TARGET_SR:.0f}s)"
            )

        # Guard against too-short clips (silence or near-silence)
        if len(audio_np) < MIN_SAMPLES:
            logger.debug(
                "Parakeet skipping short clip: %d samples (< MIN_SAMPLES=%d)",
                len(audio_np),
                MIN_SAMPLES,
            )
            return "", 0.0

        audio_mlx = mx.array(audio_np, dtype=mx.float32)
        mel = get_logmel(audio_mlx, self.model.preprocessor_config)  # type: ignore[union-attr]
        result = self.model.generate(mel, decoding_config=DecodingConfig())  # type: ignore[union-attr]
        mx.eval(result)

        text = self._extract_text(result)
        duration_ms = (time.perf_counter() - t0) * 1_000.0
        self.recent_durations.append(duration_ms)
        return text, duration_ms

    # ── public async interface ────────────────────────────────────────────────

    async def transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
        """Transcribe *audio_bytes* and return ``(text, inference_ms)``.

        Offloads to the single-thread executor so the FastAPI event loop is
        not blocked during MLX inference.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, self._sync_transcribe, audio_bytes)

    # ── metrics helpers ───────────────────────────────────────────────────────

    def p50_ms(self) -> float:
        """Return the p50 inference latency in milliseconds (0.0 if no data)."""
        if not self.recent_durations:
            return 0.0
        return float(np.percentile(list(self.recent_durations), 50))

    def p95_ms(self) -> float:
        """Return the p95 inference latency in milliseconds (0.0 if no data)."""
        if not self.recent_durations:
            return 0.0
        return float(np.percentile(list(self.recent_durations), 95))
