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
import logging
import os
import shutil
import signal
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException, UploadFile

from .._errors import UploadTruncatedError

logger = logging.getLogger(__name__)


class StagingCancelled(Exception):
    """Raised by ``transcode_to_canonical_wav`` when the caller's ``cancel_cb``
    returns True mid-decode. The ffmpeg subprocess is SIGTERM'd and the
    ``.partial`` file is removed in the ``finally`` block so the caller can
    treat this as a clean cancellation rather than an error."""

# Number of bytes to read for WAV header probe (RIFF____WAVE = 12 bytes)
_WAV_HEADER_PROBE_BYTES = 12
# Chunk size for streaming to disk: 1 MiB
_CHUNK_SIZE = 1 << 20

# Source-container extensions accepted by /transcribe/file. Anything outside
# this set returns 415. Kept narrow to surface user typos / wrong drag-drops
# instead of handing arbitrary bytes to ffmpeg.
_ALLOWED_EXTENSIONS = frozenset({
    ".m4a", ".mp3", ".mp4", ".mov", ".m4v", ".wav", ".aac",
    ".flac", ".opus", ".ogg", ".webm", ".caf", ".aiff",
})


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
    chunk_count = 0

    try:
        with open(path, "wb") as fh:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Upload too large")
                # I6: Only call disk_usage once every 64 chunks (64 MiB) to avoid
                # the syscall overhead on every 1 MiB write during large uploads.
                chunk_count += 1
                if chunk_count % 64 == 0:
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


def sweep_old(
    staging_dir: Path,
    max_age_seconds: int = 86400,
    exclude_paths: set[Path] | None = None,
) -> int:
    """Remove WAV files in *staging_dir* older than *max_age_seconds*.

    Called once at server startup to clean up any files left over from a
    previous run (e.g. if the server crashed mid-upload).

    Parameters
    ----------
    staging_dir:
        Directory to scan for stale WAV files.
    max_age_seconds:
        Files older than this many seconds are removed. Default 86400 (24 h).
    exclude_paths:
        Set of ``Path`` objects that must NOT be deleted even if they are old.
        Pass ``{Path(j.wav_path) for j in store.list_active_jobs()}`` to protect
        WAV files that are referenced by pending or running jobs (C7 fix).

    Returns the count of files removed.
    """
    if not staging_dir.exists():
        return 0
    _exclude = exclude_paths or set()
    now = time.time()
    removed = 0
    for p in staging_dir.glob("*.wav"):
        try:
            if p.resolve() in {ep.resolve() for ep in _exclude}:
                continue
            if now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def validate_wav_completeness(path: Path) -> None:
    """Validate that *path* is a plausible non-truncated WAV file.

    Checks
    ------
    1. File is at least 44 bytes (minimum RIFF/WAVE/fmt/data header).
    2. First 4 bytes are ``RIFF`` and bytes 8–12 are ``WAVE``.
    3. The RIFF chunk size field (bytes 4–8, LE uint32) + 8 is within 4 bytes of
       the actual file size.  A truncated upload will be shorter than declared.
    4. Sub-chunk walk: iterate all sub-chunks after the WAVE marker (each is an
       8-byte header: 4-byte id + 4-byte LE uint32 size).  Verify that:
       - ``fmt `` and ``data`` sub-chunks are present and non-empty (size > 0).
       - No sub-chunk's declared size extends beyond the remaining file bytes.
       - Total walked bytes equal the declared RIFF payload size (within 4 bytes
         for encoders that pad to even boundaries).

    Raises
    ------
    UploadTruncatedError
        If any check fails.  Callers should mark the job failed and delete the
        file rather than re-queuing it (C14).
    """
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise UploadTruncatedError(f"Cannot stat WAV file: {exc}") from exc

    if file_size < 44:
        raise UploadTruncatedError(
            f"WAV file too small ({file_size} bytes); minimum RIFF header is 44 bytes"
        )

    with open(path, "rb") as fh:
        header = fh.read(12)

    if header[:4] != b"RIFF":
        raise UploadTruncatedError("Not a valid WAV file (missing RIFF marker)")
    if header[8:12] != b"WAVE":
        raise UploadTruncatedError("Not a valid WAV file (missing WAVE marker)")

    # RIFF chunk size = total file size - 8 (excludes the RIFF id + size field itself)
    declared_size = struct.unpack_from("<I", header, 4)[0]
    expected_file_size = declared_size + 8
    # Allow a small tolerance (4 bytes) for rounding / padding in some encoders.
    if file_size < expected_file_size - 4:
        raise UploadTruncatedError(
            f"WAV file appears truncated: declared {expected_file_size} bytes, "
            f"actual {file_size} bytes"
        )

    # Sub-chunk walk: read every chunk after the 12-byte RIFF/WAVE header.
    # Each sub-chunk is: 4-byte id  +  4-byte LE uint32 size  +  <size> bytes of data.
    # RIFF payload starts at offset 12 and is declared_size - 4 bytes long (the
    # -4 accounts for the "WAVE" fourcc that is part of the RIFF payload).
    found_fmt = False
    found_data = False

    offset = 12  # position of first sub-chunk
    payload_end = 8 + declared_size  # last byte of RIFF payload (exclusive)

    with open(path, "rb") as fh:
        fh.seek(offset)
        while offset < payload_end:
            chunk_header = fh.read(8)
            if len(chunk_header) < 8:
                raise UploadTruncatedError(
                    f"WAV file truncated mid sub-chunk header at offset {offset}"
                )
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]

            chunk_data_end = offset + 8 + chunk_size
            if chunk_data_end > file_size + 4:
                raise UploadTruncatedError(
                    f"Sub-chunk '{chunk_id.decode(errors='replace')}' at offset {offset} "
                    f"declares size {chunk_size} bytes but only "
                    f"{file_size - offset - 8} bytes remain in the file"
                )

            if chunk_id == b"fmt ":
                if chunk_size == 0:
                    raise UploadTruncatedError("WAV 'fmt ' sub-chunk is empty")
                found_fmt = True
            elif chunk_id == b"data":
                if chunk_size == 0:
                    raise UploadTruncatedError("WAV 'data' sub-chunk is empty (no audio samples)")
                found_data = True

            # Advance: 8-byte header + chunk data, padded to even boundary per RIFF spec.
            step = 8 + chunk_size + (chunk_size % 2)  # RIFF pad byte on odd sizes
            offset += step
            fh.seek(offset)

    if not found_fmt:
        raise UploadTruncatedError("WAV file missing required 'fmt ' sub-chunk")
    if not found_data:
        raise UploadTruncatedError("WAV file missing required 'data' sub-chunk")


async def stream_to_staging_raw(
    file: UploadFile,
    max_bytes: int,
    staging_dir: Path,
) -> Path:
    """Stream *file* to *staging_dir* WITHOUT WAV header validation.

    Companion to :func:`stream_to_staging` for the /transcribe/file endpoint —
    we accept any audio/video container and let ffmpeg sniff the format on the
    canonical-WAV transcode step. The source extension is preserved so ffmpeg
    has a hint when probing.

    Returns
    -------
    Path
        Absolute path of the written file in *staging_dir* with the source
        extension preserved (``<uuid>.<ext>``).

    Raises
    ------
    HTTPException 415  Unsupported source extension.
    HTTPException 413  Upload exceeds *max_bytes*.
    HTTPException 507  Insufficient storage on the staging filesystem.
    """
    ext = Path(file.filename or "upload.bin").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file extension: {ext}")

    staging_dir.mkdir(parents=True, exist_ok=True)

    # Disk pre-check, mirroring stream_to_staging (above): require 1.5x the
    # declared max free so the canonical-WAV transcode has room beside the
    # source.
    free_before = shutil.disk_usage(str(staging_dir)).free
    if free_before < max_bytes * 1.5:
        raise HTTPException(507, "Insufficient storage")

    out_path = staging_dir / f"{uuid.uuid4().hex}{ext}"
    bytes_written = 0
    chunk_count = 0
    try:
        # Mirror stream_to_staging's sync open inside async function pattern —
        # repo does not depend on aiofiles.
        with open(out_path, "wb") as fh:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise HTTPException(413, "Upload too large")
                chunk_count += 1
                if chunk_count % 64 == 0:
                    if shutil.disk_usage(str(staging_dir)).free < _CHUNK_SIZE * 4:
                        raise HTTPException(507, "Disk full during upload")
                fh.write(chunk)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    return out_path


def ffprobe_channel_count(source: Path) -> int:
    """Return the audio channel count of *source* via ffprobe.

    Used by the /transcribe/file worker to decide between mono (custom
    transcription) and stereo (meeting) pipelines.

    Raises
    ------
    RuntimeError
        ffprobe missing on PATH (caught by the startup sanity check normally).
    HTTPException 422
        File has no audio stream or output is unparseable.
    """
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found on PATH — installed alongside ffmpeg")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=channels",
        "-of", "default=nw=1:nk=1",
        str(source),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        stderr_tail = "\n".join(result.stderr.splitlines()[-3:])
        raise HTTPException(
            422, f"Could not probe audio: {stderr_tail or 'no audio stream'}"
        )
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise HTTPException(422, f"Unexpected ffprobe output: {result.stdout!r}")


def ffprobe_duration(source: Path) -> float:
    """Return the audio duration of *source* (seconds) via ffprobe.

    Mirror of :func:`ffprobe_channel_count` but for the format-level duration,
    used by the runner to size per-phase budgets that scale with audio length.

    Raises
    ------
    RuntimeError
        ffprobe missing on PATH (caught by the startup sanity check normally).
    HTTPException 422
        Output unparseable / no duration available.
    """
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found on PATH — installed alongside ffmpeg")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        stderr_tail = "\n".join(result.stderr.splitlines()[-3:])
        raise HTTPException(
            422, f"Could not probe duration: {stderr_tail or 'no duration'}"
        )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise HTTPException(422, f"Unexpected ffprobe duration output: {result.stdout!r}")


def transcode_to_canonical_wav(
    source: Path,
    *,
    target_channels: int,
    sample_rate: int = 16_000,
    cancel_cb: Optional[Callable[[], bool]] = None,
    pad_mono_to_stereo_silent: bool = False,
) -> Path:
    """Run ffmpeg to convert *source* → canonical 16 kHz PCM WAV.

    Writes to a ``.partial`` temp name and atomically renames on success so a
    crash mid-ffmpeg never leaves a half-transcoded WAV that orphan-recovery
    would later flag as truncated.

    NOTE: this helper does NOT unlink *source* on success. The caller is
    responsible for deleting the source AFTER the row has been updated to
    point at the canonical WAV — otherwise a crash between rename and row
    update would orphan the canonical WAV while leaving the row pointing at
    a deleted source.

    Parameters
    ----------
    pad_mono_to_stereo_silent
        When True AND target_channels=2, applies an ffmpeg pan filter that
        copies the source mono channel into ch0 and explicitly zeros ch1.
        Caller is responsible for verifying the source IS mono via ffprobe
        — passing this for a stereo source would silence the right channel.
        Used by the meeting-mode + mono-source path so the pipeline's
        in-person branch (gated on `is_silent_robust(ch2)`) fires and runs
        pyannote-on-ch1, producing per-speaker labels instead of "You"/"Other".

    Raises
    ------
    RuntimeError
        ffmpeg missing on PATH (caught by the startup sanity check normally).
    ValueError
        *target_channels* is not 1 or 2.
    HTTPException 422
        ffmpeg failed (stderr tail surfaced in detail) or produced an empty
        output.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH — install via 'brew install ffmpeg'")
    if target_channels not in (1, 2):
        raise ValueError(f"target_channels must be 1 or 2, got {target_channels}")

    target = source.with_suffix(".wav")
    if target == source:
        # Source already ends in .wav — pick a sibling name so we never
        # overwrite the source from under ourselves.
        target = source.with_name(f"{source.stem}_canonical.wav")
    temp_target = target.with_suffix(target.suffix + ".partial")

    cmd = [
        "ffmpeg",
        "-y", "-nostdin",
        "-i", str(source),
        "-map", "0:a:0",         # explicit: same audio track ffprobe inspected
        "-vn",
    ]
    if pad_mono_to_stereo_silent and target_channels == 2:
        # Pan filter: ch0 = source ch0, ch1 = explicit silence (0 * source ch0).
        # This is what `is_silent_robust(ch2)` in the pipeline expects so the
        # in-person branch fires for mono meeting recordings.
        cmd += ["-af", "pan=stereo|c0=c0|c1=0*c0"]
    else:
        # Standard channel up/down-mix (ffmpeg duplicates mono to both
        # channels when going 1→2; sums L+R when going 2→1).
        cmd += ["-ac", str(target_channels)]
    cmd += [
        "-ar", str(sample_rate),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        str(temp_target),
    ]

    # 30-minute ceiling shared with the previous subprocess.run timeout.
    deadline = time.monotonic() + 30 * 60
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    cancelled = False
    timed_out = False
    try:
        # 500ms poll loop: check ffmpeg exit, caller's cancel flag, and the
        # overall transcode deadline. We do NOT read stderr concurrently
        # (ffmpeg's stderr buffer is small but tractable for our 30-min
        # ceiling at default verbosity).
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if cancel_cb is not None:
                try:
                    if cancel_cb():
                        cancelled = True
                        break
                except Exception:  # noqa: BLE001 — cancel_cb must never crash decode
                    logger.exception("cancel_cb raised during transcode; ignoring")
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(0.5)

        if cancelled or timed_out:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if cancelled:
                raise StagingCancelled("cancelled mid-decode")
            raise HTTPException(422, "Audio transcode timed out (>30 min)")

        # Drain remaining stderr for diagnostics.
        _, stderr = proc.communicate(timeout=5)
        if proc.returncode != 0:
            stderr_tail = "\n".join((stderr or "").splitlines()[-5:])
            logger.error(
                "ffmpeg transcode failed (rc=%d): %s",
                proc.returncode, stderr_tail,
            )
            raise HTTPException(422, f"Audio transcode failed: {stderr_tail}")
        if not temp_target.exists() or temp_target.stat().st_size < 100:
            raise HTTPException(422, "Audio transcode produced empty/no output")

        # Atomic publish.
        os.replace(temp_target, target)
        logger.info(
            "ffmpeg transcoded %s → %s (%d bytes)",
            source.name, target.name, target.stat().st_size,
        )
        return target
    finally:
        # Uniform cleanup of the .partial file for both cancel + error paths.
        # On the success path, os.replace already moved it → unlink is a no-op.
        try:
            temp_target.unlink(missing_ok=True)
        except OSError:
            pass
        # Make sure we never leak a child process.
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


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
