"""
dictate/v1_response_builders.py — OpenAI-compatible response builders for /v1.

Produces json/text/verbose_json/srt/vtt response bodies from a Parakeet transcription.
Reuses SRT/VTT timecode formatters from meeting/output.py.
"""
from __future__ import annotations

import re
from typing import Any

from wispralt_server.meeting.output import _seconds_to_srt, _seconds_to_vtt

# Look back over the last few tokens' joined text for a sentence-end.
# Parakeet emits subword tokens — a single token rarely ends in [.!?].
SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s*$")
SENTENCE_LOOKBACK = 4
TIME_GAP_THRESHOLD_S = 0.5
MAX_SEGMENT_SECONDS = 12.0
MAX_SEGMENT_TOKENS = 80
MIN_SEGMENT_SECONDS = 1.0


def _group_into_segments(aligned_tokens: list) -> list[dict[str, Any]]:
    """Split AlignedToken list into OpenAI-shaped segments.

    Boundary rules (in order):
      - time gap > 0.5s between prev_token.end and cur_token.start → ALWAYS split
      - sentence-end in last SENTENCE_LOOKBACK tokens' joined text → split IF segment ≥ 1s
      - segment exceeds MAX_SEGMENT_SECONDS or MAX_SEGMENT_TOKENS → force split
    """
    if not aligned_tokens:
        return []
    segments: list[dict[str, Any]] = []
    current_tokens: list = []
    seg_start = float(aligned_tokens[0].start)
    last_end = float(aligned_tokens[0].end)
    seg_id = 0

    def flush() -> None:
        nonlocal current_tokens, seg_id, seg_start
        if not current_tokens:
            return
        text = "".join(str(t.text) for t in current_tokens).strip()
        if not text:
            current_tokens = []
            return
        segments.append({
            "id": seg_id,
            "seek": 0,
            "start": float(seg_start),
            "end": float(current_tokens[-1].end),
            "text": text,
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
            "transient": False,
        })
        seg_id += 1
        current_tokens = []

    for tok in aligned_tokens:
        cur_start = float(tok.start)
        cur_end = float(tok.end)
        if current_tokens:
            time_gap = cur_start - last_end
            seg_duration = last_end - seg_start
            tail_text = "".join(str(t.text) for t in current_tokens[-SENTENCE_LOOKBACK:])
            should_split = (
                time_gap > TIME_GAP_THRESHOLD_S
                or seg_duration >= MAX_SEGMENT_SECONDS
                or len(current_tokens) >= MAX_SEGMENT_TOKENS
                or (
                    SENTENCE_END.search(tail_text)
                    and seg_duration >= MIN_SEGMENT_SECONDS
                )
            )
            if should_split:
                flush()
                seg_start = cur_start
        current_tokens.append(tok)
        last_end = cur_end
    flush()
    return segments


def build_verbose_json(
    text: str,
    duration_s: float,
    aligned_tokens: list | None,
    include_words: bool,
) -> dict[str, Any]:
    """Build the OpenAI verbose_json response body.

    `language` is the lowercase full English name ("english"), NOT "en" —
    strict clients deserializing into typed structs require the long form.
    `transient: false` on every segment — undocumented but always emitted by
    real OpenAI; strict clients fail without it.
    """
    if not text:
        body: dict[str, Any] = {
            "task": "transcribe",
            "language": "english",
            "duration": float(duration_s),
            "text": "",
            "segments": [],
        }
        if include_words:
            body["words"] = []
        return body

    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
        if include_words:
            words: list[dict[str, Any]] | None = [
                {
                    "word": str(t.text).strip(),
                    "start": float(t.start),
                    "end": float(t.end),
                }
                for t in aligned_tokens
                if str(t.text).strip()
            ]
        else:
            words = None
    else:
        # Hypothesis (text-only, no alignment) — single degenerate segment
        segments = [{
            "id": 0,
            "seek": 0,
            "start": 0.0,
            "end": float(duration_s),
            "text": text,
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
            "transient": False,
        }]
        words = [] if include_words else None

    body = {
        "task": "transcribe",
        "language": "english",
        "duration": float(duration_s),
        "text": text,
        "segments": segments,
    }
    if words is not None:
        body["words"] = words
    return body


def build_srt(text: str, duration_s: float, aligned_tokens: list | None) -> str:
    """Build SubRip SRT body. Comma decimal separator."""
    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
    elif text:
        segments = [{"start": 0.0, "end": float(duration_s), "text": text}]
    else:
        return ""
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(
            f"{_seconds_to_srt(seg['start'])} --> {_seconds_to_srt(seg['end'])}"
        )
        lines.append(str(seg["text"]).strip())
        lines.append("")
    return "\n".join(lines)


def build_vtt(text: str, duration_s: float, aligned_tokens: list | None) -> str:
    """Build WebVTT body. Period decimal separator + WEBVTT magic line."""
    if aligned_tokens:
        segments = _group_into_segments(aligned_tokens)
    elif text:
        segments = [{"start": 0.0, "end": float(duration_s), "text": text}]
    else:
        return "WEBVTT\n"
    parts: list[str] = ["WEBVTT", ""]
    for seg in segments:
        parts.append(
            f"{_seconds_to_vtt(seg['start'])} --> {_seconds_to_vtt(seg['end'])}"
        )
        parts.append(str(seg["text"]).strip())
        parts.append("")
    return "\n".join(parts)
