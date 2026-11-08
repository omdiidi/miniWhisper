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
- MLX (``mlx``, ``parakeet_mlx``) is imported lazily inside ``_load_mlx`` /
  ``_clear_mlx_cache``, so ``wispralt.main`` imports without the ``[host]``
  extra. Tests inject ``ParakeetService(load_fn=…)``: ``load_fn(model_id)``
  returns ``run(samples) -> result`` and the service never touches MLX.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

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
from wispralt.config import settings as _settings
MAX_SAMPLES = TARGET_SR * _settings.dictation_max_duration_s
# Clear the MLX cache after every clip longer than this, else every Nth request.
CACHE_CLEAR_MIN_SAMPLES = TARGET_SR * 30
CACHE_CLEAR_EVERY_N = 20

Runner = Callable[[np.ndarray], object]
LoadFn = Callable[[str], Runner]


def _clear_mlx_cache() -> None:
    """Return MLX's unified-memory pool to the OS (a missing API never breaks dictation)."""
    import mlx.core as mx

    with contextlib.suppress(AttributeError):  # a moved MLX API must not break dictation
        mx.metal.clear_cache()


def _load_mlx(model_id: str) -> Runner:
    """Load Parakeet on MLX, run a warmup pass (Metal JIT), return the runner."""
    import mlx.core as mx
    from parakeet_mlx import DecodingConfig, from_pretrained  # type: ignore[import-untyped]
    from parakeet_mlx.audio import get_logmel  # type: ignore[import-untyped]

    model = from_pretrained(model_id, dtype=mx.bfloat16)

    def run(samples: np.ndarray) -> object:
        audio_mlx = mx.array(samples, dtype=mx.float32)
        mel = get_logmel(audio_mlx, model.preprocessor_config)
        result = model.generate(mel, decoding_config=DecodingConfig())
        mx.eval(result)
        del mel, audio_mlx  # so a following cache clear can release them
        return result

    # Warmup forces Metal kernel compilation so the first real request is not
    # penalised (300 ms-2 s). Then release the pool so the post-load baseline
    # reflects only the persistent weights (~1.2 GB bf16).
    logger.info("Running Parakeet warmup pass …")
    run(np.zeros(TARGET_SR // 2, dtype=np.float32))
    _clear_mlx_cache()
    return run


class ParakeetService:
    """Warm-loaded Parakeet transcription service.

    Lifecycle::

        service = ParakeetService()
        service.load()            # called once in FastAPI lifespan
        text, ms = await service.transcribe(wav_bytes)
    """

    def __init__(self, load_fn: LoadFn | None = None) -> None:
        self.model_id: str = MODEL_ID
        self._load_fn: LoadFn = load_fn or _load_mlx
        self._clear_cache: Callable[[], None] = (
            _clear_mlx_cache if load_fn is None else (lambda: None)
        )
        self._run: Runner | None = None
        self.ready: bool = False
        # Single-thread executor — MLX model is NOT thread-safe
        self._exec: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet")
        # Rolling window of recent inference durations (milliseconds), maxlen=100 for percentiles
        self.recent_durations: collections.deque[float] = collections.deque(maxlen=100)
        self._requests_since_clear: int = 0

    def load(self) -> None:
        """Load the model (default: Parakeet on MLX, with a warmup pass).

        Must be called from the main thread (or any single thread) before the
        first ``transcribe`` call.
        """
        logger.info("Loading Parakeet model %s …", self.model_id)
        self._run = self._load_fn(self.model_id)
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

        aligned_tokens is None when result is a Hypothesis-shaped object
        OR a list of items that lack per-token start/end timestamps
        (e.g. parakeet-mlx ``AlignedResult`` wrappers — they expose ``.text``
        but not ``.start``/``.end`` at the top level).

        aligned_tokens is a list ONLY when every item exposes the full
        ``.text`` + ``.start`` + ``.end`` triple — the shape needed for
        verbose_json segmentation and SRT/VTT cue boundaries.

        Used by /v1 verbose_json/srt/vtt segmentation.
        """
        if (
            isinstance(result, list)
            and result
            and hasattr(result[0], "text")
            and hasattr(result[0], "start")
            and hasattr(result[0], "end")
        ):
            text = "".join(str(t.text) for t in result).strip()
            return text, list(result)
        # List but items lack timestamps — extract text only, no segmentation.
        if isinstance(result, list) and result and hasattr(result[0], "text"):
            text = "".join(str(t.text) for t in result).strip()
            return text, None
        # Single Hypothesis-shaped object with .text.
        if hasattr(result, "text"):
            return str(result.text).strip(), None
        logger.warning("Unexpected Parakeet result type: %s", type(result))
        return "", None

    def _run_model(self, samples: np.ndarray) -> object:
        """Run the loaded model on 16 kHz mono float32 *samples*; return the raw result."""
        if self._run is None:
            raise RuntimeError("ParakeetService.load() has not run")
        return self._run(samples)

    def _maybe_clear_cache(self, n_samples: int) -> None:
        """Return MLX's unified-memory pool to the OS, but not on every request.

        MLX grows the pool to the largest working set seen and never shrinks it
        on its own, so long clips (big peaks) clear immediately. Short clips
        reuse the cached buffers (clearing costs ~10-30 ms of re-allocation on
        the next call), so for those we clear only every
        ``CACHE_CLEAR_EVERY_N`` requests to bound drift.
        """
        self._requests_since_clear += 1
        if (
            n_samples <= CACHE_CLEAR_MIN_SAMPLES
            and self._requests_since_clear < CACHE_CLEAR_EVERY_N
        ):
            return
        self._requests_since_clear = 0
        self._clear_cache()

    def _sync_transcribe(self, audio_bytes: bytes) -> tuple[str, float]:
        """Blocking inference — runs inside the single-thread executor.

        Decodes + resamples the WAV bytes, then delegates to
        :meth:`_sync_transcribe_with_alignment` (which applies the length
        guards, runs the model and records the duration).

        Returns
        -------
        (text, duration_ms)
        """
        # Decode + resample at the audio.py boundary — every decode/resample
        # failure becomes CorruptAudioError so the route layer maps it to 422.
        audio_np, sr = decode_wav_bytes(audio_bytes)

        # Flatten stereo to mono
        if audio_np.ndim == 2:
            audio_np = audio_np.mean(axis=1)

        # Resample to 16 kHz; safe_resample also maps librosa errors to 422.
        audio_np = safe_resample(audio_np, sr, TARGET_SR)

        # MAX_SAMPLES (decode-amplification) and MIN_SAMPLES guards live in
        # the shared method below.
        text, duration_ms, _ = self._sync_transcribe_with_alignment(audio_np)
        return text, duration_ms

    def _sync_transcribe_with_alignment(
        self, samples: np.ndarray,
    ) -> tuple[str, float, list | None]:
        """Run inference on pre-decoded samples; returns aligned tokens.

        Used by /v1/audio/transcriptions (own libsndfile/ffmpeg decode) and by
        :meth:`_sync_transcribe`.

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
        from wispralt._errors import AudioTooLongError

        t0 = time.perf_counter()

        if len(samples) > MAX_SAMPLES:
            raise AudioTooLongError(
                f"Audio too long: {len(samples) / TARGET_SR:.1f}s "
                f"(max {MAX_SAMPLES / TARGET_SR:.0f}s). For longer audio, use "
                f"Transcribe file in the menu bar."
            )
        if len(samples) < MIN_SAMPLES:
            logger.debug(
                "Parakeet skipping short clip: %d samples (< MIN_SAMPLES=%d)",
                len(samples), MIN_SAMPLES,
            )
            return "", 0.0, None

        result = self._run_model(samples)
        text, tokens = self._extract_text_and_tokens(result)
        del result
        self._maybe_clear_cache(len(samples))

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
