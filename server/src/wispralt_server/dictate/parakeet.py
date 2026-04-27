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
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import librosa
import mlx.core as mx
import numpy as np
import soundfile as sf

from parakeet_mlx import from_pretrained, DecodingConfig  # type: ignore[import-untyped]
from parakeet_mlx.audio import get_logmel  # type: ignore[import-untyped]

from .._errors import CorruptAudioError

logger = logging.getLogger(__name__)

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
TARGET_SR = 16_000
# Minimum samples required for a meaningful transcription (100ms of audio at 16kHz)
MIN_SAMPLES = 1_600


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

        # Decode audio — convert any soundfile decode failure to CorruptAudioError
        # so the route layer can map it to 422 instead of leaking as a 500.
        audio_np: np.ndarray
        try:
            audio_np, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
        except (sf.LibsndfileError, RuntimeError) as exc:
            raise CorruptAudioError(f"Cannot decode audio: {exc}") from exc

        # Flatten stereo to mono
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1)

        # Resample to 16 kHz if necessary
        if sr != TARGET_SR:
            audio_np = librosa.resample(audio_np, orig_sr=sr, target_sr=TARGET_SR)

        # Guard against too-short clips (silence or near-silence)
        if len(audio_np) < MIN_SAMPLES:
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
