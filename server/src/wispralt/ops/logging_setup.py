"""
ops/logging_setup.py — size-capped application log.

launchd opens ``server.log`` / ``server.err.log`` once and appends forever, and
cannot rotate them without sudo (newsyslog). So the app writes its own log via
a ``RotatingFileHandler`` (``settings.server_log_path``) and the launchd files
only receive crash output (import errors, native tracebacks, stray prints).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_file_logging(path: Path, max_bytes: int, backup_count: int) -> bool:
    """Route root + uvicorn loggers to a size-rotated file.

    Replaces the stderr handlers so the launchd err file stops growing.
    Returns False (leaving stderr logging untouched) if the file can't be
    opened, so a bad path never prevents startup.
    """
    root = logging.getLogger()
    target = str(path.resolve())
    existing = next(
        (
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and h.baseFilename == target
        ),
        None,
    )
    if existing is not None:
        # Idempotent: reuse the handler, just make sure uvicorn points at it.
        _attach_uvicorn(existing)
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    except OSError:
        logging.getLogger(__name__).exception(
            "Could not open %s; continuing with stderr logging", path
        )
        return False
    handler.setFormatter(logging.Formatter(_FORMAT))

    for h in list(root.handlers):
        if not isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
    root.addHandler(handler)
    _attach_uvicorn(handler)
    return True


def _attach_uvicorn(handler: logging.Handler) -> None:
    # uvicorn installs its own stderr handlers with propagate=False.
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
