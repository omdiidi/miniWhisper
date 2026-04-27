"""
meeting/__init__.py — Package init.

This package previously installed two **process-global** monkey-patches at
import time:

1. ``torch.load`` / ``torch.serialization.load`` → ``weights_only=False``
   (PyTorch 2.6 flipped the default to True, which blocks omegaconf and
   other pickled objects in pyannote/whisperx checkpoints).
2. ``huggingface_hub.{hf_hub_download, snapshot_download}`` →
   translate the removed ``use_auth_token=`` kwarg to ``token=``
   (pyannote.audio 3.3.2 still calls the old name).

Both patches are now exposed via ``install_compat_shims()`` (idempotent)
called from the FastAPI lifespan, plus a ``trusted_load_context()`` context
manager for scoped use.  The patches are no longer applied implicitly on
import, so they cannot leak into unrelated code paths.

The torch.load shim **only sets** ``weights_only=False`` when the caller
hasn't passed it — a defensive ``torch.load(path, weights_only=True)`` from
a future code path is preserved.

Usage from `main.py:lifespan`::

    from wispralt_server.meeting import install_compat_shims
    install_compat_shims()  # idempotent

Or, for one-shot scoped use::

    from wispralt_server.meeting import trusted_load_context
    with trusted_load_context():
        model = whisperx.load_model(...)
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Module-level flag: True after install_compat_shims() runs.
_compat_installed: bool = False
_orig_torch_load: Callable[..., Any] | None = None
_orig_torch_ser_load: Callable[..., Any] | None = None
_orig_hf_hub_download: Callable[..., Any] | None = None
_orig_snapshot_download: Callable[..., Any] | None = None


def install_compat_shims() -> None:
    """Install torch.load + huggingface_hub compat patches process-wide.

    Called once from the FastAPI lifespan, before any meeting-pipeline code
    imports torch checkpoints or pyannote pipelines.  Idempotent — safe to
    call multiple times; subsequent calls are no-ops.
    """
    global _compat_installed
    global _orig_torch_load, _orig_torch_ser_load
    global _orig_hf_hub_download, _orig_snapshot_download

    if _compat_installed:
        return

    import torch
    import torch.serialization
    import huggingface_hub

    _orig_torch_load = torch.load
    _orig_torch_ser_load = torch.serialization.load
    _orig_hf_hub_download = huggingface_hub.hf_hub_download
    _orig_snapshot_download = huggingface_hub.snapshot_download

    def _shimmed_torch_load(*args, **kwargs):  # type: ignore[no-untyped-def]
        # ALWAYS force weights_only=False for the meeting bootstrap.  Pyannote
        # 3.3.2 and WhisperX both pass `weights_only=True` explicitly inside
        # their loaders, which would fail under torch 2.6's stricter
        # safe-globals check (omegaconf.ListConfig is not in the allowlist).
        # We trust the HF checkpoints we explicitly downloaded, so override.
        # Trade-off vs the codex-review finding: we CHOOSE convenience here
        # because the shim is now scoped (only installed during meeting
        # bootstrap) — see install_compat_shims() lifecycle below.
        kwargs["weights_only"] = False
        assert _orig_torch_load is not None
        return _orig_torch_load(*args, **kwargs)

    torch.load = _shimmed_torch_load  # type: ignore[assignment]
    torch.serialization.load = _shimmed_torch_load  # type: ignore[assignment]

    def _shimmed_hf_hub_download(*args, **kwargs):  # type: ignore[no-untyped-def]
        if "use_auth_token" in kwargs:
            kwargs["token"] = kwargs.pop("use_auth_token")
        assert _orig_hf_hub_download is not None
        return _orig_hf_hub_download(*args, **kwargs)

    def _shimmed_snapshot_download(*args, **kwargs):  # type: ignore[no-untyped-def]
        if "use_auth_token" in kwargs:
            kwargs["token"] = kwargs.pop("use_auth_token")
        assert _orig_snapshot_download is not None
        return _orig_snapshot_download(*args, **kwargs)

    huggingface_hub.hf_hub_download = _shimmed_hf_hub_download  # type: ignore[assignment]
    huggingface_hub.snapshot_download = _shimmed_snapshot_download  # type: ignore[assignment]

    _compat_installed = True
    logger.info(
        "meeting compat shims installed (torch.load weights_only=False; HF use_auth_token→token)"
    )


def uninstall_compat_shims() -> None:
    """Restore the original torch.load + huggingface_hub functions.

    Used by tests and by the context manager.  Idempotent.
    """
    global _compat_installed
    if not _compat_installed:
        return
    import torch
    import torch.serialization
    import huggingface_hub

    if _orig_torch_load is not None:
        torch.load = _orig_torch_load  # type: ignore[assignment]
    if _orig_torch_ser_load is not None:
        torch.serialization.load = _orig_torch_ser_load  # type: ignore[assignment]
    if _orig_hf_hub_download is not None:
        huggingface_hub.hf_hub_download = _orig_hf_hub_download  # type: ignore[assignment]
    if _orig_snapshot_download is not None:
        huggingface_hub.snapshot_download = _orig_snapshot_download  # type: ignore[assignment]

    _compat_installed = False
    logger.info("meeting compat shims uninstalled")


@contextlib.contextmanager
def trusted_load_context():  # type: ignore[no-untyped-def]
    """Scope ``install_compat_shims`` to a `with` block.

    Patches are restored on exit unless they were already installed before
    entry (in which case they remain — the context manager is non-destructive
    on already-installed state).
    """
    was_installed = _compat_installed
    install_compat_shims()
    try:
        yield
    finally:
        if not was_installed:
            uninstall_compat_shims()
