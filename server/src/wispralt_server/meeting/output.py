"""
meeting/output.py — Atomic transcript writers (JSON, SRT, VTT, TXT).

All four formats are written via a tempfile-in-same-dir → os.replace pattern
so that readers never see a partially written file.

Format conventions (locked in docs/TRANSCRIPT-FORMAT.md):
- SRT body:  ``Speaker: text``
- VTT body:  ``<v Speaker>text</v>`` (voice tags for native speaker display)
- TXT body:  ``[Speaker] text`` per segment, one per line

Timecode formats:
- SRT:  ``HH:MM:SS,mmm``
- VTT:  ``HH:MM:SS.mmm``
"""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# ── private helpers ────────────────────────────────────────────────────────────


def _atomic_write(content: bytes, dest: Path) -> None:
    """Write *content* to *dest* atomically via a temp file in the same directory.

    Steps:
    1. Create a temp file in ``dest.parent`` (same filesystem → rename is
       atomic on APFS/HFS+/ext4).
    2. Write, flush, fsync.
    3. ``chmod 644`` so the file is readable by the serving process.
    4. Assert the temp file is actually in the intended directory (defence
       against path-traversal bugs).
    5. ``os.replace`` (atomic rename).

    On any failure the temp file is cleaned up and the exception re-raised so
    the caller sees the original error.
    """
    tmp = tempfile.NamedTemporaryFile(
        dir=dest.parent,
        delete=False,
        suffix=".tmp",
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        # M8: Use 0o600 (more conservative) rather than 0o644 for transcript files.
        # chmod BEFORE replace so the file is never visible at dest with wrong perms.
        os.chmod(tmp.name, 0o600)
        # Defend against path-traversal: temp file must be in dest.parent.
        assert os.path.dirname(os.path.abspath(tmp.name)) == os.path.abspath(
            str(dest.parent)
        ), (
            f"Temp file {tmp.name!r} is not inside output directory "
            f"{dest.parent!r} — aborting atomic write."
        )
        try:
            os.replace(tmp.name, dest)
        except OSError as exc:
            # A2: EXDEV means tmp file and dest are on different filesystems.
            # os.replace() cannot atomically rename across devices; log CRITICAL.
            if getattr(exc, "errno", None) == errno.EXDEV:
                logger.critical(
                    "FATAL: os.replace('%s' -> '%s') failed with EXDEV — "
                    "STAGING_DIR and MEETING_OUTPUT_DIR must be on the same filesystem.",
                    tmp.name,
                    dest,
                )
            raise RuntimeError(
                f"Atomic rename failed ({exc}); ensure output_dir and tmp dir are on the same filesystem."
            ) from exc
    except Exception:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise


def _seconds_to_srt(seconds: float) -> str:
    """Convert *seconds* to SRT timecode ``HH:MM:SS,mmm``."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _seconds_to_vtt(seconds: float) -> str:
    """Convert *seconds* to VTT timecode ``HH:MM:SS.mmm``."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ── format writers ─────────────────────────────────────────────────────────────


def write_json(transcript: dict, dest: Path) -> None:
    """Write the full transcript dict as pretty-printed UTF-8 JSON."""
    content = json.dumps(transcript, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(content, dest)


def write_srt(transcript: dict, dest: Path) -> None:
    """Write the transcript as an SRT subtitle file.

    Each segment becomes one SRT cue::

        1
        00:00:00,000 --> 00:00:03,420
        Speaker: text
    """
    lines: list[str] = []
    for i, seg in enumerate(transcript.get("segments", []), start=1):
        start_tc = _seconds_to_srt(float(seg.get("start", 0.0)))
        end_tc = _seconds_to_srt(float(seg.get("end", 0.0)))
        speaker = seg.get("speaker", "")
        text = seg.get("text", "").strip()
        lines.append(str(i))
        lines.append(f"{start_tc} --> {end_tc}")
        lines.append(f"{speaker}: {text}")
        lines.append("")  # blank line between cues

    content = "\n".join(lines).encode("utf-8")
    _atomic_write(content, dest)


def write_vtt(transcript: dict, dest: Path) -> None:
    """Write the transcript as a WebVTT file using voice tags.

    Each segment becomes one VTT cue::

        00:00:00.000 --> 00:00:03.420
        <v Speaker>text</v>
    """
    lines: list[str] = ["WEBVTT", ""]
    for seg in transcript.get("segments", []):
        start_tc = _seconds_to_vtt(float(seg.get("start", 0.0)))
        end_tc = _seconds_to_vtt(float(seg.get("end", 0.0)))
        speaker = seg.get("speaker", "")
        text = seg.get("text", "").strip()
        lines.append(f"{start_tc} --> {end_tc}")
        lines.append(f"<v {speaker}>{text}</v>")
        lines.append("")  # blank line between cues

    content = "\n".join(lines).encode("utf-8")
    _atomic_write(content, dest)


def write_txt(transcript: dict, dest: Path) -> None:
    """Write the transcript as a plain-text file.

    One line per segment::

        [Speaker] text
    """
    lines: list[str] = []
    for seg in transcript.get("segments", []):
        speaker = seg.get("speaker", "")
        text = seg.get("text", "").strip()
        lines.append(f"[{speaker}] {text}")

    content = "\n".join(lines).encode("utf-8")
    _atomic_write(content, dest)


# ── stale tmp sweep ───────────────────────────────────────────────────────────


def sweep_stale_tmp(output_dir: Path, max_age_seconds: int = 3600) -> int:
    """Remove ``*.tmp`` files in *output_dir* older than *max_age_seconds*.

    Called once at server startup (after recover_orphans + sweep_old) to clean
    up any temp files left by a previous crash during an atomic write.

    Returns the count of files removed (I8).
    """
    if not output_dir.exists():
        return 0
    now = time.time()
    removed = 0
    for p in output_dir.glob("*.tmp"):
        try:
            if now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                removed += 1
                logger.debug("Removed stale tmp file: %s", p)
        except OSError:
            pass
    return removed


# ── orchestration ──────────────────────────────────────────────────────────────


def write_outputs_atomic(
    transcript: dict,
    output_dir: Path,
    job_id: str,
) -> dict[str, Path]:
    """Write all four transcript formats atomically to *output_dir*.

    Creates *output_dir* (and any missing parents) if it does not exist.
    Files are named ``{job_id}.{json,srt,vtt,txt}``.

    Parameters
    ----------
    transcript:
        Full transcript dict conforming to the locked v3 schema.
    output_dir:
        Directory to write output files into.
    job_id:
        UUID job identifier used as the file basename.

    Returns
    -------
    dict[str, Path]
        Mapping of format name → absolute Path for each written file.

    Raises
    ------
    DiskFullError
        If any write fails with ``errno.ENOSPC``.
    OSError
        For all other filesystem errors.
    """
    from wispralt_server._errors import DiskFullError

    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / job_id
    paths: dict[str, Path] = {}

    writers = [
        ("json", write_json),
        ("srt", write_srt),
        ("vtt", write_vtt),
        ("txt", write_txt),
    ]

    try:
        for fmt, writer in writers:
            dest = base.with_suffix(f".{fmt}")
            writer(transcript, dest)
            paths[fmt] = dest
            logger.debug("Wrote %s → %s", fmt.upper(), dest)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ENOSPC:
            raise DiskFullError(str(exc)) from exc
        raise

    return paths
