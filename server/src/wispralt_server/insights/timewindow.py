"""Pure helpers for ISO-week math + time-range tab math.

No FastAPI / Jinja / DB deps. Imported by routes/me.py, routes/admin_data.py,
and insights/cron.py so all three share one source of truth for week
boundaries (previously inlined 3x with subtle drift risk on DST).
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def last_full_iso_week(tz_name: str) -> tuple[int, int]:
    """ISO (year, week) of the most recently completed week in ``tz_name``."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    days_since_monday = (now.isoweekday() - 1) % 7
    this_monday = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    last_monday = this_monday - timedelta(days=7)
    iso_year, iso_week, _ = last_monday.isocalendar()
    return iso_year, iso_week


def iso_week_epoch_bounds(
    iso_year: int, iso_week: int, tz_name: str,
) -> tuple[float, float]:
    """Return ``(week_start_epoch, week_end_epoch)`` for the given ISO week."""
    tz = ZoneInfo(tz_name)
    monday = datetime.fromisocalendar(iso_year, iso_week, 1).replace(tzinfo=tz)
    next_monday = monday + timedelta(days=7)
    return monday.timestamp(), next_monday.timestamp()


def epoch_for_range(range_: str, tz_name: str) -> float:
    """Lower-bound UNIX epoch for a time-range tab value.

    ``range_`` is one of: today, 7d, 30d, 90d, 1y, all.
    Unknown values default to 7d.
    """
    if range_ == "today":
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": 36500}
    days = days_map.get(range_, 7)
    return _time.time() - days * 86400


VALID_RANGES: frozenset[str] = frozenset({"today", "7d", "30d", "90d", "1y", "all"})
