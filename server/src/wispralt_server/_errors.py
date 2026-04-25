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
