"""
_errors.py — Typed domain exceptions for WisprAlt server.

All exceptions raised at service boundaries (not at request-validation
boundaries) are rooted here so callers can catch them specifically.
"""

from __future__ import annotations


class DiskFullError(Exception):
    """Raised when a write fails because the filesystem has no free space (ENOSPC)."""


class CorruptAudioError(Exception):
    """Raised when uploaded audio bytes cannot be decoded as a valid audio file."""


class UploadTruncatedError(Exception):
    """Raised when the uploaded file is shorter than its declared WAV header size."""


class MeetingInProgressError(Exception):
    """Raised when a new meeting job is submitted while one is already running."""


class AudioTooLongError(CorruptAudioError):
    """Audio sample count exceeds MAX_SAMPLES. 400-mapped on /v1.

    Subclass of CorruptAudioError so existing `except CorruptAudioError` callers
    (routes/dictate.py, routes/dictate_stream.py) keep working unchanged.
    Distinguish via isinstance() at the /v1 boundary BEFORE the broader catch.
    """


class UnsupportedAudioError(Exception):
    """ffmpeg cannot decode the supplied container/codec. 400-mapped on /v1."""


class DecodeTimeoutError(Exception):
    """ffmpeg decode exceeded the per-request budget (60s). 400-mapped on /v1.

    NOT 408 — openai-python retries 408. Map to 400 so SDK doesn't retry.
    """
