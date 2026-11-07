"""
files/segments.py — Segment labelling for file transcripts.

All functions in this module are pure (no I/O, no model calls).  They operate
on whisper result dicts (from mlx_whisper.transcribe) and return lists of
segment dicts that conform to the locked v3 transcript schema.

Locked v3 segment schema
------------------------
Each segment dict contains:
    start       float   — start time in seconds
    end         float   — end time in seconds
    channel     int|None — channel number, or None for a single mono stream
    speaker     str     — current display name (denormalized; rewritten on rename)
    speaker_raw str     — stable raw label (file jobs always use "mic")
    text        str     — transcript text for this segment
    words       list    — per-word dicts (empty: file jobs run without word timestamps)
    overlap     bool    — True when this segment starts before the previous ended

Speaker table (keyed by ``speaker_raw``)
-----------------------------------------
    {"mic": {"display_name": "Speaker 1", "channel": None}}
"""

from __future__ import annotations


def _make_segment(
    *,
    start: float,
    end: float,
    channel: int | None,
    speaker: str,
    speaker_raw: str,
    text: str,
    words: list[dict],
    overlap: bool,
) -> dict:
    """Construct a single segment dict conforming to the v3 schema."""
    return {
        "start": start,
        "end": end,
        "channel": channel,
        "speaker": speaker,
        "speaker_raw": speaker_raw,
        "text": text,
        "words": words,
        "overlap": overlap,
    }


def label_all(
    whisper_result: dict,
    display_name: str,
    channel: int | None,
    raw_speakers: list[str],
) -> list[dict]:
    """Tag every segment with a fixed *display_name* and the first raw label.

    File jobs label every segment "Speaker 1" (one mono stream).

    Parameters
    ----------
    whisper_result:
        A whisper-result dict from mlx_whisper.transcribe (has a ``"segments"`` key).
    display_name:
        The display name to assign to every segment (e.g. ``"Speaker 1"``).
    channel:
        Channel number or None (None for in-person mode).
    raw_speakers:
        List of raw speaker labels to record.  The *first* element is used as
        ``speaker_raw`` on every segment (e.g. ``["mic"]``).

    Returns
    -------
    list[dict]
        List of segment dicts with ``overlap=False``.
    """
    raw = raw_speakers[0] if raw_speakers else "mic"
    segments: list[dict] = []
    for seg in whisper_result.get("segments", []):
        segments.append(
            _make_segment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                channel=channel,
                speaker=display_name,
                speaker_raw=raw,
                text=seg.get("text", "").strip(),
                words=list(seg.get("words", [])),
                overlap=False,
            )
        )
    return segments


def build_speakers_table(segments: list[dict]) -> dict[str, dict]:
    """Build the ``speakers`` table (keyed by ``speaker_raw``) from a segment list.

    The table maps each raw label to its display name and channel so the
    client can render and rename speakers without re-parsing every segment.

    Returns
    -------
    dict
        ``{speaker_raw: {"display_name": str, "channel": int | None}, …}``
    """
    table: dict[str, dict] = {}
    for seg in segments:
        raw = seg["speaker_raw"]
        if raw not in table:
            table[raw] = {
                "display_name": seg["speaker"],
                "channel": seg["channel"],
            }
    return table
