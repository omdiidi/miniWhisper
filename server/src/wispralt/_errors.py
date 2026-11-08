"""
_errors.py — Typed domain exceptions for WisprAlt server.

All exceptions raised at service boundaries (not at request-validation
boundaries) are rooted here so callers can catch them specifically.
"""

from __future__ import annotations


class MigrationError(Exception):
    """The DB's applied migrations do not match this code's migration files."""


class AlreadyBootstrapped(Exception):
    """First-admin bootstrap refused: an enabled admin already exists."""


class DedupConflict(Exception):
    """The dictation dedup id is already stored for another user (HTTP 409 ``dedup_conflict``)."""


class DictationDeleted(Exception):
    """The dictation stored under this dedup id was deleted by its owner (HTTP 410 ``dictation_deleted``)."""


class DiskFullError(Exception):
    """Raised when a write fails because the filesystem has no free space (ENOSPC)."""


class CorruptAudioError(Exception):
    """Raised when uploaded audio bytes cannot be decoded as a valid audio file."""


class UploadTruncatedError(Exception):
    """Raised when the uploaded file is shorter than its declared WAV header size."""


class FileJobInProgressError(Exception):
    """Raised when a file job cannot start (one already running, or low memory).

    ``code`` is the machine-readable reason carried in the 429 body so the
    client can branch without matching on the message text:
    ``"file_job_busy"`` or ``"insufficient_memory"``.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


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
