"""meeting/mlx_whisper_loader.py — mlx-whisper one-shot loader (Phase 1 swap).

One-shot transcribe (preserves Whisper's cross-window prompting). Progress
reporting via ``tqdm.auto.tqdm.update`` monkey-patch with a wall-clock
fallback that emits synthetic estimates if no tqdm activity is observed
within 60 s.

Verified (Phase 0 spike): mlx-whisper==0.4.2 imports ``from tqdm.auto
import tqdm`` and progress is visible during transcribe. The patched module
path is ``tqdm.auto.tqdm``.

Module-level ``_patch_lock`` serialises the monkey-patch so concurrent
invocations cannot interleave the temporary attribute swap. Today the
runner serialises with Semaphore(1) so this is defensive only.

Public surface (matches whisperx_loader.py):
    load() -> None
    reset() -> None
    transcribe_channel(audio_16k, *, word_timestamps=False,
                       progress_cb=None, cancel_cb=None,
                       duration_s_override=None) -> dict
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

import numpy as np

logger = logging.getLogger(__name__)

# Default model: mlx-community whisper-large-v3-turbo. May be swapped to
# the non-turbo large-v3 variant per Phase 0 T0.5 if accuracy regresses.
_MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

_loaded: bool = False
_patch_lock = threading.Lock()


def load() -> None:
    """Warm the mlx-whisper model with a 1-second silence transcribe.

    Sets ``_loaded = True`` on success. Subsequent calls are no-ops. Called
    from the meeting executor thread on first meeting job via
    ``pipeline._ensure_models_loaded()``.
    """
    global _loaded
    if _loaded:
        return
    import mlx_whisper  # type: ignore[import-untyped]

    logger.info("Warming mlx-whisper model %s …", _MODEL_REPO)
    silence = np.zeros(16_000, dtype=np.float32)
    _ = mlx_whisper.transcribe(
        silence,
        path_or_hf_repo=_MODEL_REPO,
        word_timestamps=False,
        language="en",
        verbose=False,
    )
    _loaded = True
    logger.info("mlx-whisper ready.")


def reset() -> None:
    """Drop the loaded flag. MLX uses unified memory so this is essentially
    a no-op for RAM reclaim — the OS will reuse pages on demand. Kept for
    API symmetry with whisperx_loader.reset()."""
    global _loaded
    _loaded = False


def transcribe_channel(
    audio_16k: np.ndarray,
    *,
    word_timestamps: bool = False,
    progress_cb: Callable[[str, int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    duration_s_override: float | None = None,
) -> dict:
    """Transcribe *audio_16k* (1-D float32 @ 16 kHz) and return an mlx-whisper
    result dict (segments, optional words[]).

    Parameters
    ----------
    audio_16k:
        1-D float32 PCM array sampled at 16 kHz.
    word_timestamps:
        Pass-through to mlx_whisper. True for meeting mode (needed for
        speaker-boundary segment splits in merge.assign_speakers_segments);
        False for file mode (~20 % perf savings).
    progress_cb:
        Optional ``(phase: str, idx: int, total: int) -> None``. Phase is
        always ``"transcribe"`` from this module. Exceptions raised by
        ``progress_cb`` are logged and swallowed so transcribe continues.
    cancel_cb:
        Optional ``() -> bool``. mlx-whisper cannot be interrupted mid-decode
        from Python, so we only log a warning if the cancel flag flips during
        transcribe; the user-visible UI surfaces this via the "Previous job
        finishing on server" banner.
    duration_s_override:
        Optional explicit duration; otherwise computed from ``len(audio_16k)
        / 16000``.

    Returns
    -------
    dict
        ``{"segments": [...], ...}`` per mlx-whisper. When
        ``word_timestamps=True`` each segment has a ``words`` list.
    """
    import mlx_whisper  # type: ignore[import-untyped]
    import tqdm.auto as _tqdm_mod  # type: ignore[import-untyped]

    duration_s = duration_s_override if duration_s_override is not None else (
        len(audio_16k) / 16_000
    )
    total_windows = max(1, math.ceil(duration_s / 30.0))

    # Synthetic-progress fallback: if tqdm.update never fires within 60 s
    # we start emitting an estimate every 5 s assuming ~5× realtime decode.
    fallback_state: dict[str, object] = {"saw_tqdm": False, "stop": False}

    def fallback_emitter() -> None:
        t_start = time.monotonic()
        while not fallback_state["stop"]:
            time.sleep(5)
            if fallback_state["stop"]:
                return
            if fallback_state["saw_tqdm"]:
                return  # real progress is firing; we're not needed
            elapsed = time.monotonic() - t_start
            if elapsed < 60:
                continue  # give tqdm a chance to fire first
            if progress_cb is None:
                continue
            # Synthetic estimate: assume 5× realtime. Per-window ≈ duration/total/5.
            per_window = max(6.0, duration_s / total_windows / 5)
            est_done = min(total_windows, int(elapsed / per_window))
            try:
                progress_cb("transcribe", est_done, total_windows)
            except Exception:
                logger.exception("synthetic progress_cb raised — continuing")

    chunk_counter = {"n": 0}

    with _patch_lock:
        original_update = _tqdm_mod.tqdm.update

        def patched_update(self: object, n: int = 1) -> object:  # type: ignore[no-untyped-def]
            chunk_counter["n"] += n
            fallback_state["saw_tqdm"] = True
            if progress_cb is not None:
                try:
                    progress_cb(
                        "transcribe",
                        min(chunk_counter["n"], total_windows),
                        total_windows,
                    )
                except Exception:
                    logger.exception("progress_cb raised — continuing transcribe")
            if cancel_cb is not None:
                try:
                    if cancel_cb():
                        logger.warning(
                            "cancel requested but mlx-whisper cannot be "
                            "interrupted mid-decode; flag is advisory"
                        )
                except Exception:
                    logger.exception("cancel_cb raised — continuing transcribe")
            return original_update(self, n)

        _tqdm_mod.tqdm.update = patched_update  # type: ignore[method-assign]
        fb_thread = threading.Thread(target=fallback_emitter, daemon=True)
        fb_thread.start()
        try:
            result: dict = mlx_whisper.transcribe(
                audio_16k,
                path_or_hf_repo=_MODEL_REPO,
                word_timestamps=word_timestamps,
                language="en",
                hallucination_silence_threshold=2.0,
                verbose=False,
            )
        finally:
            fallback_state["stop"] = True
            _tqdm_mod.tqdm.update = original_update  # type: ignore[method-assign]
    return result
