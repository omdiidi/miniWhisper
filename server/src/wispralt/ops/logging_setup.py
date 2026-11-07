"""
ops/logging_setup.py — size-capped application log + repeated-failure dedupe.

launchd opens ``server.log`` / ``server.err.log`` once and appends forever, and
cannot rotate them without sudo (newsyslog). So the app writes its own log via
a ``RotatingFileHandler`` (``settings.server_log_path``) and the launchd files
only receive crash output (import errors, native tracebacks, stray prints).
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path
from typing import Callable

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


class RepeatedFailureLogger:
    """Log the first occurrence of a failure in full, then only a periodic summary.

    A watcher loop that fails every 10 s would otherwise emit a traceback per
    iteration (23,000 of them in one past outage). Failures are keyed; a new
    key logs in full immediately, repeats of the same key are counted and
    summarised at most once per ``summary_interval_s``. ``recovered()`` flushes
    the count and re-arms.
    """

    def __init__(
        self,
        logger: logging.Logger,
        summary_interval_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._interval = summary_interval_s
        self._clock = clock
        self._key: str | None = None
        self._suppressed = 0
        self._last_emit = 0.0

    def failure(self, key: str, msg: str, *args: object, exc_info: bool = False) -> bool:
        """Record a failure. Returns True if it was logged in full."""
        now = self._clock()
        if key != self._key:
            self._flush_summary()
            self._key = key
            self._suppressed = 0
            self._last_emit = now
            self._logger.warning(msg, *args, exc_info=exc_info)
            return True
        self._suppressed += 1
        if now - self._last_emit >= self._interval:
            self._logger.warning(
                "%s [repeated %d more time(s) in the last %.0f s; "
                "traceback suppressed]",
                msg % args if args else msg,
                self._suppressed,
                now - self._last_emit,
            )
            self._suppressed = 0
            self._last_emit = now
        return False

    def recovered(self) -> None:
        """Call when the operation succeeds; logs any suppressed count and resets."""
        self._flush_summary()
        self._key = None
        self._suppressed = 0

    def _flush_summary(self) -> None:
        if self._key is not None and self._suppressed:
            self._logger.info(
                "Suppressed %d repeat(s) of: %s", self._suppressed, self._key
            )
