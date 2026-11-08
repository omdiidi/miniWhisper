"""
insights/measured.py — measured stats: numbers computed from DB rows, never by the LLM.

Always shown, with AI on or off. ``compute`` is the one service call the pages use
(sync: run it in ``asyncio.to_thread``); everything else is a pure function over
repository rows so it can be tested against a hand-built fixture.

What is measured (and what is not):
- counts, words and active days come from ``dictations`` / ``jobs`` rows;
- file audio minutes come from ``jobs.audio_duration_s`` (dictation rows store
  inference time, not audio length, so dictation audio is not claimed);
- filler words are a regex count over dictation text (``FILLER_RE``);
- latency p50/p95 and the error rate come from ``usage_events`` (request telemetry);
- "time saved" is an ESTIMATE against a 40 wpm typing baseline, and is labelled so.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from wispralt.usage.events import DICTATE, WHOLE_REQUEST_KINDS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from wispralt.jobs.store import JobStore
    from wispralt.usage.store import UsageStore

TYPING_WPM = 40
TREND_WEEKS = 8
FILLERS = ("um", "uh", "like", "you know")
# "umm", "uhh" fold into um / uh. "like" over-counts real uses; the page says which words count.
FILLER_RE = re.compile(r"\b(?:(um+)|(uh+)|(like)|(you know))\b", re.IGNORECASE)


def count_fillers(texts: Iterable[str]) -> dict[str, int]:
    """``{"um": n, "uh": n, "like": n, "you know": n, "total": n}`` over *texts*."""
    counts: Counter[str] = Counter()
    for text in texts:
        for m in FILLER_RE.finditer(text or ""):
            counts[FILLERS[m.lastindex - 1]] += 1
    out = {f: counts[f] for f in FILLERS}
    out["total"] = sum(counts.values())
    return out


def time_saved_hours(words: int) -> float:
    """Estimate only: the minutes typing *words* at ``TYPING_WPM`` would take, in hours."""
    return round(words / TYPING_WPM / 60.0, 2)


def _local(ts: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def active_days(points: Iterable[tuple[float, int]], tz_name: str) -> int:
    tz = ZoneInfo(tz_name)
    return len({_local(ts, tz).date() for ts, _ in points})


def hour_histogram(points: Iterable[tuple[float, int]], tz_name: str) -> list[int]:
    """24 counts: records per local hour of day."""
    tz = ZoneInfo(tz_name)
    hours = [0] * 24
    for ts, _ in points:
        hours[_local(ts, tz).hour] += 1
    return hours


def trend_start(now: float, tz_name: str, weeks: int = TREND_WEEKS) -> float:
    """Local Monday 00:00 of the first week in a *weeks*-long trend ending this week."""
    tz = ZoneInfo(tz_name)
    today = _local(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    return (monday - timedelta(weeks=weeks - 1)).timestamp()


def weekly_trend(
    points: Iterable[tuple[float, int]], now: float, tz_name: str, weeks: int = TREND_WEEKS
) -> list[dict[str, Any]]:
    """One bucket per ISO week (oldest first, this week last): records and words."""
    tz = ZoneInfo(tz_name)
    first = datetime.fromtimestamp(trend_start(now, tz_name, weeks), tz)
    buckets = []
    for i in range(weeks):
        y, w, _ = (first + timedelta(weeks=i)).isocalendar()
        buckets.append({"iso_year": y, "iso_week": w, "label": f"W{w:02d}", "count": 0, "words": 0})
    index = {(b["iso_year"], b["iso_week"]): b for b in buckets}
    for ts, words in points:
        y, w, _ = _local(ts, tz).isocalendar()
        b = index.get((y, w))
        if b is not None:
            b["count"] += 1
            b["words"] += words
    return buckets


@dataclass(frozen=True, slots=True)
class Window:
    since: float
    until: float


def compute(
    job_store: JobStore,
    usage: UsageStore,
    *,
    user_id: int | None,
    since: float,
    until: float,
    now: float,
    tz_name: str,
) -> dict[str, Any]:
    """Every measured number for one scope (a person, or ``None`` = the team) and window."""
    window = job_store.measured_window(user_id, since, until)
    start = min(since, trend_start(now, tz_name))
    points = job_store.activity_points(user_id, start, max(until, now))
    in_window = [p for p in points if since <= p[0] < until]
    fillers = count_fillers(text for _uid, text in job_store.iter_dictation_texts(user_id, since, until))
    # Whole requests only: a streamed dictation's chunk rows are pieces of the one
    # finalize row, so counting them would skew the request count and error rate.
    telemetry = usage.latency_error_summary(
        since, until, user_id=user_id, kinds=WHOLE_REQUEST_KINDS
    )
    latency = telemetry["by_kind"].get(DICTATE) or {}
    return {
        "window": Window(since, until),
        "dictation_count": window["dictation_count"],
        "file_count": window["file_count"],
        "words": window["words"],
        "file_audio_minutes": round(window["file_audio_s"] / 60.0, 1),
        "dictation_inference_s": round(window["dictation_inference_ms"] / 1000.0, 1),
        "active_days": active_days(in_window, tz_name),
        "fillers": fillers,
        "time_saved_hours": time_saved_hours(window["words"]),
        "latency_p50_ms": latency.get("p50_ms"),
        "latency_p95_ms": latency.get("p95_ms"),
        "requests": telemetry["total"],
        "error_rate": telemetry["error_rate"],
        "trend": weekly_trend(points, now, tz_name),
        "hours": hour_histogram(in_window, tz_name),
    }


def fillers_by_user(job_store: JobStore, since: float, until: float) -> dict[int, int]:
    """Team leaderboard: measured filler totals per user over the window."""
    totals: Counter[int] = Counter()
    for uid, text in job_store.iter_dictation_texts(None, since, until):
        totals[uid] += sum(1 for _ in FILLER_RE.finditer(text or ""))
    return dict(totals)
