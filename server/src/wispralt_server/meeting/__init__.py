"""
meeting/__init__.py — Package init.

PyTorch 2.6 flipped torch.load default to weights_only=True. WhisperX,
pyannote, and the fairseq wav2vec2 alignment checkpoint pickle omegaconf
objects (and others) that would have to be allowlisted exhaustively. All
models loaded by this package come from HF repos we explicitly downloaded
and whose terms we accepted, so force weights_only=False for trusted loads.

This patch must run before any submodule imports whisperx/pyannote/torch
checkpoints — which is why it lives in the package __init__.
"""

from __future__ import annotations

import torch

_orig_torch_load = torch.load


def _trusted_torch_load(*args, **kwargs):  # type: ignore[no-untyped-def]
    # Force-disable; pyannote checkpoints set weights_only=True explicitly.
    kwargs["weights_only"] = False
    return _orig_torch_load(*args, **kwargs)


torch.load = _trusted_torch_load  # type: ignore[assignment]
# Also patch the qualified path used internally by some libs.
import torch.serialization as _ts  # noqa: E402

_ts.load = _trusted_torch_load  # type: ignore[assignment]

# pyannote.audio 3.3.2 calls hf_hub_download(use_auth_token=...). The kwarg
# was renamed to `token` in huggingface_hub >= 0.26 and removed entirely.
# Translate the legacy kwarg before delegating.
import huggingface_hub as _hfh  # noqa: E402

_orig_hf_hub_download = _hfh.hf_hub_download
_orig_snapshot_download = _hfh.snapshot_download


def _compat_hf_hub_download(*args, **kwargs):  # type: ignore[no-untyped-def]
    if "use_auth_token" in kwargs:
        kwargs["token"] = kwargs.pop("use_auth_token")
    return _orig_hf_hub_download(*args, **kwargs)


def _compat_snapshot_download(*args, **kwargs):  # type: ignore[no-untyped-def]
    if "use_auth_token" in kwargs:
        kwargs["token"] = kwargs.pop("use_auth_token")
    return _orig_snapshot_download(*args, **kwargs)


_hfh.hf_hub_download = _compat_hf_hub_download  # type: ignore[assignment]
_hfh.snapshot_download = _compat_snapshot_download  # type: ignore[assignment]
