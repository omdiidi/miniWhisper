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

logger = logging.getLogger(__name__)

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"
TARGET_SR = 16_000
# Minimum samples required for a meaningful transcription (100ms of audio at 16kHz)
MIN_SAMPLES = 1_600
# Hard cap on post-resample length, configured via DICTATION_MAX_DURATION_S
# (default 300s = 5 min). Dictation is a hold-FN gesture; previous default of
# 60s wrongly rejected legitimate long-form dictations. The cap still defends
# against decode-amplification (1KB body decoding to many minutes) and single-
# thread executor starvation. Read once at import to avoid per-request settings
# attribute lookup on the hot path.
from wispralt_server.config import settings as _settings
MAX_SAMPLES = TARGET_SR * _settings.dictation_max_duration_s


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
        # Drop warmup tensors and release the pool so the post-load baseline
        # reflects only persistent model weights (~1.2 GB for Parakeet 0.6B
        # bf16) — not whatever transient peaks the warmup pass needed.
        del result, mel, dummy
        try:
            mx.metal.clear_cache()
        except AttributeError:
            pass

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

    def _extract_text_and_tokens(self, result: object) -> tuple[str, list | None]:
        """Return (text, aligned_tokens_or_none).

        aligned_tokens is None when result is a Hypothesis (text-only),
        or a list of AlignedToken objects when alignment is surfaced.
        Used by /v1 verbose_json segmentation.
        """
        if isinstance(result, list) and result and hasattr(result[0], "text"):
            text = "".join(str(t.text) for t in result).strip()
            return text, list(result)
        if hasattr(result, "text"):
            return str(result.text).strip(), None
        logger.warning("Unexpected Parakeet result type: %s", type(result))
        return "", None

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
            from wispralt_server._errors import AudioTooLongError
            raise AudioTooLongError(
                f"Audio too long: {len(audio_np) / TARGET_SR:.1f}s "
                f"(max {MAX_SAMPLES / TARGET_SR:.0f}s). For longer audio, use "
                f"meeting recording (FN-triple-tap)."
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

        # Drop refs before clearing the MLX pool so the cache release actually
        # returns memory to the OS. Without explicit `del`, Python keeps the
        # tensors alive until the next assignment and clear_cache() is a no-op
        # for the bytes those tensors own.
        del result, mel, audio_mlx
        # Return MLX's unified-memory pool to the OS. MLX grows the pool to
        # accommodate the largest working set seen so far (a 2-min dictation
        # peaks several GB) and NEVER shrinks it on its own. Without this call
        # the python process accumulates ~all-time-peak unified memory and the
        # mini's "Memory" column climbs monotonically per dictation. Clearing
        # adds ~10-30 ms to the next inference (re-allocating from OS) which
        # is well below the dictation budget and dwarfed by the inference itself.
        try:
            mx.metal.clear_cache()
        except AttributeError:
            # Future MLX may move the API; don't crash dictation over a memory
            # hygiene call.
            pass

        duration_ms = (time.perf_counter() - t0) * 1_000.0
        self.recent_durations.append(duration_ms)
        return text, duration_ms

    def _sync_transcribe_with_alignment(
        self, samples: np.ndarray,
    ) -> tuple[str, float, list | None]:
        """Variant of _sync_transcribe taking pre-decoded samples; returns aligned tokens.

        Used by /v1/audio/transcriptions which performs its own libsndfile/ffmpeg
        decode before this call.

        Parameters
        ----------
        samples
            16 kHz mono float32 ndarray. Caller is responsible for decoding +
            resampling + downmixing.

        Returns
        -------
        (text, inference_ms, aligned_tokens_or_None)
            aligned_tokens is the parakeet-mlx AlignedToken list when the model
            returned per-token alignment, or None when only a Hypothesis was returned.
        """
        from wispralt_server._errors import AudioTooLongError

        t0 = time.perf_counter()

        if len(samples) > MAX_SAMPLES:
            raise AudioTooLongError(
                f"Audio too long: {len(samples) / TARGET_SR:.1f}s "
                f"(max {MAX_SAMPLES / TARGET_SR:.0f}s). For longer audio, use "
                f"meeting recording (FN-triple-tap)."
            )
        if len(samples) < MIN_SAMPLES:
            logger.debug(
                "Parakeet skipping short clip: %d samples (< MIN_SAMPLES=%d)",
                len(samples), MIN_SAMPLES,
            )
            return "", 0.0, None

        audio_mlx = mx.array(samples, dtype=mx.float32)
        mel = get_logmel(audio_mlx, self.model.preprocessor_config)  # type: ignore[union-attr]
        result = self.model.generate(mel, decoding_config=DecodingConfig())  # type: ignore[union-attr]
        mx.eval(result)

        text, tokens = self._extract_text_and_tokens(result)

        del result, mel, audio_mlx
        try:
            mx.metal.clear_cache()
        except AttributeError:
            pass

        duration_ms = (time.perf_counter() - t0) * 1_000.0
        self.recent_durations.append(duration_ms)
        return text, duration_ms, tokens

    # ── public async interface ────────────────────────────────────────────────

    async def transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
        """Transcribe *audio_bytes* and return ``(text, inference_ms)``.

        Offloads to the single-thread executor so the FastAPI event loop is
        not blocked during MLX inference.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, self._sync_transcribe, audio_bytes)

    async def transcribe_with_alignment(
        self, samples: np.ndarray,
    ) -> tuple[str, float, list | None]:
        """Async wrapper around _sync_transcribe_with_alignment.

        Offloads to the single-thread executor (same as `transcribe`) so the
        FastAPI event loop stays free during MLX inference.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._exec, self._sync_transcribe_with_alignment, samples,
        )

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
