"""
meeting/merge.py — Segment labelling and channel merge for meeting transcripts.

All functions in this module are pure (no I/O, no model calls).  They operate
on WhisperX-style result dicts and return lists of segment dicts that conform
to the locked v3 transcript schema.

Locked v3 segment schema
------------------------
Each segment dict contains:
    start       float   — start time in seconds
    end         float   — end time in seconds
    channel     int|None — 1=mic, 2=system, None=in-person mode
    speaker     str     — current display name (denormalized; rewritten on rename)
    speaker_raw str     — stable raw label (pyannote "SPEAKER_NN" or "mic")
    text        str     — transcript text for this segment
    words       list    — per-word dicts: {word, start, end, score}
    overlap     bool    — True when this segment starts before the previous ended

Speaker table (keyed by ``speaker_raw``)
-----------------------------------------
    {
      "mic":        {"display_name": "You",     "channel": 1},
      "SPEAKER_00": {"display_name": "Other",   "channel": 2},
      "SPEAKER_01": {"display_name": "Other 2", "channel": 2},
    }
"""

from __future__ import annotations

import re

_WHITESPACE_RUN = re.compile(r"\s+")


def _join_words(items: list[tuple[dict, str]]) -> str:
    # mlx-whisper word fields often carry a leading space ("·hello"); joining
    # with " " then yields runs of 2+ spaces in TXT output. Collapse any
    # whitespace run to a single space.
    return _WHITESPACE_RUN.sub(
        " ",
        " ".join(str(item[0].get("word", "")) for item in items),
    ).strip()


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
    whisperx_result: dict,
    display_name: str,
    channel: int | None,
    raw_speakers: list[str],
) -> list[dict]:
    """Tag every segment with a fixed *display_name* and the first raw label.

    Used for channel 1 (mic) in remote mode ("You") or when in-person
    diarization found nothing ("Speaker 1").

    Parameters
    ----------
    whisperx_result:
        A WhisperX aligned-result dict (has a ``"segments"`` key).
    display_name:
        The display name to assign to every segment (e.g. ``"You"``).
    channel:
        Channel number or None (None for in-person mode).
    raw_speakers:
        List of raw speaker labels to record.  The *first* element is used as
        ``speaker_raw`` on every segment (e.g. ``["mic"]``).

    Returns
    -------
    list[dict]
        List of segment dicts with ``overlap=False`` (caller or
        ``merge_two_channels`` will set overlap flags).
    """
    raw = raw_speakers[0] if raw_speakers else "mic"
    segments: list[dict] = []
    for seg in whisperx_result.get("segments", []):
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


def label_others(
    diarized_result: dict,
    base: str = "Other",
    channel: int = 2,
) -> list[dict]:
    """Map Pyannote speaker labels to "Other", "Other 2", "Other 3", etc.

    ``SPEAKER_00`` → *base* (e.g. "Other"), ``SPEAKER_01`` → "Other 2",
    ``SPEAKER_02`` → "Other 3", and so on.

    Parameters
    ----------
    diarized_result:
        A WhisperX result dict after ``whisperx.assign_word_speakers``.  Each
        segment may have a ``"speaker"`` key with a Pyannote raw label.
    base:
        The display name for the first speaker (index 0).
    channel:
        Channel number (2 for system audio in remote mode).

    Returns
    -------
    list[dict]
        Segment dicts with ``overlap=False``.
    """
    # Build a stable mapping from raw Pyannote label → display name.
    label_map: dict[str, str] = {}

    def _display_for(raw: str) -> str:
        if raw in label_map:
            return label_map[raw]
        idx = len(label_map)
        display = base if idx == 0 else f"{base} {idx + 1}"
        label_map[raw] = display
        return display

    segments: list[dict] = []
    for seg in diarized_result.get("segments", []):
        raw_label: str = seg.get("speaker", "SPEAKER_00")
        display = _display_for(raw_label)
        segments.append(
            _make_segment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                channel=channel,
                speaker=display,
                speaker_raw=raw_label,
                text=seg.get("text", "").strip(),
                words=list(seg.get("words", [])),
                overlap=False,
            )
        )
    return segments


def relabel_in_person(
    diarized_result: dict,
    channel: int | None = None,
) -> list[dict]:
    """Map Pyannote labels to "Speaker 1", "Speaker 2", etc.

    ``SPEAKER_00`` → "Speaker 1", ``SPEAKER_01`` → "Speaker 2", …

    Parameters
    ----------
    diarized_result:
        A WhisperX result dict after ``whisperx.assign_word_speakers``.
    channel:
        Channel number; None for in-person mode.

    Returns
    -------
    list[dict]
        Segment dicts with ``overlap=False``.
    """
    label_map: dict[str, str] = {}

    def _display_for(raw: str) -> str:
        if raw in label_map:
            return label_map[raw]
        idx = len(label_map)
        display = f"Speaker {idx + 1}"
        label_map[raw] = display
        return display

    segments: list[dict] = []
    for seg in diarized_result.get("segments", []):
        raw_label: str = seg.get("speaker", "SPEAKER_00")
        display = _display_for(raw_label)
        segments.append(
            _make_segment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                channel=channel,
                speaker=display,
                speaker_raw=raw_label,
                text=seg.get("text", "").strip(),
                words=list(seg.get("words", [])),
                overlap=False,
            )
        )
    return segments


def merge_two_channels(
    ch1_segs: list[dict],
    ch2_segs: list[dict],
) -> list[dict]:
    """Merge two channel segment lists into a single chronologically sorted list.

    Segments are sorted by ``start`` time.  After sorting, the ``overlap``
    flag is set to True on any segment whose ``start`` is earlier than the
    ``end`` of the preceding segment (simultaneous speech).

    Parameters
    ----------
    ch1_segs:
        Segments from channel 1 (mic / "You").
    ch2_segs:
        Segments from channel 2 (system audio / "Other …").

    Returns
    -------
    list[dict]
        Merged, chronologically sorted segment list with ``overlap`` flags
        correctly set.
    """
    merged = sorted(ch1_segs + ch2_segs, key=lambda s: s["start"])

    prev_end: float = 0.0
    for seg in merged:
        seg["overlap"] = seg["start"] < prev_end
        if seg["end"] > prev_end:
            prev_end = seg["end"]

    return merged


def assign_speakers_segments(
    transcribe_result: dict,
    diar_df: object,
) -> list[dict]:
    """Map each transcribed segment to a pyannote speaker label.

    Replaces ``whisperx.assign_word_speakers`` after the mlx-whisper swap
    (Phase 1). Two strategies:

    1. **Word-aware split** when ``word_timestamps=True`` (meeting mode):
       group consecutive words by their assigned speaker, emit one segment
       per speaker run with start/end clipped to the word boundaries.
    2. **Largest-overlap fallback** when no word timestamps are present
       (file mode never reaches this code path, but kept for safety):
       assign the dominant pyannote speaker (largest temporal overlap)
       and do not split.

    The returned segments are **raw** — they still carry pyannote
    "SPEAKER_NN" labels in the ``speaker`` field. The caller
    (``relabel_in_person`` or ``label_others``) maps those to display
    names. To keep that contract the output dict mirrors mlx-whisper's
    segment dict shape (``start``, ``end``, ``text``, ``speaker``,
    ``words``).

    Parameters
    ----------
    transcribe_result:
        mlx-whisper result dict with ``segments``. Each segment may carry
        a ``words`` list when ``word_timestamps=True``.
    diar_df:
        pandas DataFrame from pyannote diarization. Required columns:
        ``start`` (float), ``end`` (float), ``speaker`` (str).

    Returns
    -------
    list[dict]
        Segment dicts shaped like mlx-whisper segments with an added
        ``speaker`` key (raw pyannote label, e.g. "SPEAKER_00", or
        "Unknown" if no overlap was found).
    """
    out_segments: list[dict] = []
    for seg in transcribe_result.get("segments", []):
        words = seg.get("words", []) or []
        if words:
            # Word-aware split: group consecutive words by assigned speaker.
            assigned: list[tuple[dict, str]] = []
            for w in words:
                t = (float(w["start"]) + float(w["end"])) / 2
                matched = diar_df[
                    (diar_df["start"] <= t) & (diar_df["end"] >= t)
                ]
                if len(matched):
                    speaker = str(matched["speaker"].iloc[0])
                else:
                    speaker = "Unknown"
                assigned.append((w, speaker))

            if not assigned:
                continue

            current: list[tuple[dict, str]] = [assigned[0]]
            for w, sp in assigned[1:]:
                if sp == current[-1][1]:
                    current.append((w, sp))
                else:
                    out_segments.append(
                        {
                            "start": float(current[0][0]["start"]),
                            "end": float(current[-1][0]["end"]),
                            "text": _join_words(current),
                            "speaker": current[0][1],
                            "words": [item[0] for item in current],
                        }
                    )
                    current = [(w, sp)]
            # Flush last group
            out_segments.append(
                {
                    "start": float(current[0][0]["start"]),
                    "end": float(current[-1][0]["end"]),
                    "text": _join_words(current),
                    "speaker": current[0][1],
                    "words": [item[0] for item in current],
                }
            )
        else:
            # Largest-overlap, no splitting.
            best_speaker = "Unknown"
            best_overlap = 0.0
            seg_start = float(seg.get("start", 0.0))
            seg_end = float(seg.get("end", 0.0))
            for _, row in diar_df.iterrows():
                ov = max(
                    0.0,
                    min(seg_end, float(row["end"]))
                    - max(seg_start, float(row["start"])),
                )
                if ov > best_overlap:
                    best_overlap = ov
                    best_speaker = str(row["speaker"])
            out_segments.append({**seg, "speaker": best_speaker})
    return out_segments


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
