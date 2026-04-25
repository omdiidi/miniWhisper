"""
ops/staging.py — Staging-directory management for meeting WAV uploads.

Responsibilities:
- Stream an upload to disk in 1 MB chunks with disk-space pre/mid-checks (P5#10).
- Validate the WAV RIFF/WAVE header after write (P5#4).
- Optionally verify Content-MD5 (P5#15 integrity).
- Cleanup helpers: per-job cleanup and age-based sweep.
- assert_same_filesystem: ensure staging and output dirs share one FS device so
  that atomic tempfile-rename (os.replace) in output.py is truly atomic
  (R1#15 + P4#11).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .._errors import UploadTruncatedError

# Number of bytes to read for WAV header probe (RIFF____WAVE = 12 bytes)
_WAV_HEADER_PROBE_BYTES = 12
# Chunk size for streaming to disk: 1 MiB
_CHUNK_SIZE = 1 << 20


async def stream_to_staging(
    file: UploadFile,
    max_bytes: int,
    staging_dir: Path,
    content_md5_b64: str | None = None,
) -> tuple[Path, str]:
    """Stream *file* to *staging_dir* in 1 MiB chunks.

    Parameters
    ----------
    file:
        FastAPI UploadFile received from the multipart request.
    max_bytes:
        Hard size limit; raises HTTP 413 if exceeded.
    staging_dir:
        Directory where the WAV will be written (created if absent).
    content_md5_b64:
        Optional base64-encoded MD5 from the ``Content-MD5`` request header.
        If provided and mismatches, HTTP 422 is raised and the partial file is
        removed.

    Returns
    -------
    (path, md5_b64)
        *path* is the absolute Path to the written WAV.
        *md5_b64* is the base64-encoded MD5 of the bytes written (regardless of
        whether *content_md5_b64* was supplied).

    Raises
    ------
    HTTPException 413  Upload exceeds *max_bytes*.
    HTTPException 422  WAV header invalid or Content-MD5 mismatch.
    HTTPException 507  Insufficient storage (pre-check or mid-upload).
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    # P5#10: disk-free pre-check — require at least 1.5× the declared max so we
    # have headroom for the WAV plus any temp files.
    free_before = shutil.disk_usage(str(staging_dir)).free
    if free_before < max_bytes * 1.5:
        raise HTTPException(507, "Insufficient storage")

    path = staging_dir / f"{uuid.uuid4()}.wav"
    md5 = hashlib.md5()
    total = 0

    try:
        with open(path, "wb") as fh:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Upload too large")
                # P5#10: mid-upload disk guard — leave at least 4 chunks of
                # headroom so we don't fill the filesystem to zero.
                if shutil.disk_usage(str(staging_dir)).free < _CHUNK_SIZE * 4:
                    raise HTTPException(507, "Disk full during upload")
                md5.update(chunk)
                fh.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    # Compute observed MD5 and verify if caller supplied one
    md5_observed = base64.b64encode(md5.digest()).decode("ascii")
    if content_md5_b64 is not None and content_md5_b64.strip() != md5_observed:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "Content-MD5 mismatch")

    # P5#4: WAV header sanity — must start with RIFF and have WAVE at offset 8.
    try:
        with open(path, "rb") as fh:
            head = fh.read(_WAV_HEADER_PROBE_BYTES)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise UploadTruncatedError("Not a valid WAV (missing RIFF/WAVE header)")
    except UploadTruncatedError:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "Upload truncated; please retry")

    return path, md5_observed


def cleanup(wav_path: Path) -> None:
    """Remove *wav_path* silently.  Called after a job finishes (R1#2)."""
    try:
        wav_path.unlink(missing_ok=True)
    except OSError:
        pass


def sweep_old(staging_dir: Path, max_age_seconds: int = 86400) -> int:
    """Remove WAV files in *staging_dir* older than *max_age_seconds*.

    Called once at server startup to clean up any files left over from a
    previous run (e.g. if the server crashed mid-upload).

    Returns the count of files removed.
    """
    if not staging_dir.exists():
        return 0
    now = time.time()
    removed = 0
    for p in staging_dir.glob("*.wav"):
        try:
            if now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def assert_same_filesystem(a: Path, b: Path) -> None:
    """Raise RuntimeError if *a* and *b* are on different filesystems.

    R1#15 + P4#11: staging and output directories must share one device so
    that ``os.replace`` in ``output.py`` is truly atomic (a POSIX rename(2)
    across filesystems would silently fall back to copy+delete and lose
    atomicity).

    Both directories are created (parents included) before the ``os.stat``
    comparison so the check works even before the first job is run.
    """
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    if os.stat(a).st_dev != os.stat(b).st_dev:
        raise RuntimeError(
            f"STAGING_DIR ({a}) and MEETING_OUTPUT_DIR ({b}) must be on the"
            " same filesystem for atomic file renames to be reliable."
        )
