"""Shared helpers for the history/records pages (``/me/history`` and ``/admin/records``).

Pure presentation helpers: the time window a page shows and the row dicts its
templates render. Access control is NOT here: it lives in the ``JobStore`` SQL.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from wispralt.insights.timewindow import epoch_for_range

if TYPE_CHECKING:
    from wispralt.jobs.store import HistoryHit

MIN_QUERY_CHARS = 2


def window(range_: str, since: float | None, until: float | None, tz_name: str) -> tuple[float, float]:
    """``since``/``until`` (unix seconds) win over the ``range`` tab when given."""
    lo = since if since is not None else epoch_for_range(range_, tz_name)
    hi = until if until is not None else time.time()
    return lo, hi


def wants_search(q: str | None) -> str | None:
    """The trimmed query when it is long enough to search, else None (plain list)."""
    q = (q or "").strip()
    return q if len(q) >= MIN_QUERY_CHARS else None


def iso_local(ts: float | None, tz_name: str) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), ZoneInfo(tz_name)).isoformat(timespec="minutes")
    except (ValueError, OSError):
        return ""


def decorate(row: dict[str, Any], tz_name: str) -> dict[str, Any]:
    """New dict with ``created_at_iso`` for the templates."""
    return {**row, "created_at_iso": iso_local(row.get("created_at"), tz_name)}


def hit_row(hit: HistoryHit, tz_name: str) -> dict[str, Any]:
    """A search hit as a history row. ``snippet`` stays RAW: templates use ``|highlight``."""
    return {
        "kind": hit.kind,
        "row_id": hit.row_id,
        "user_id": hit.user_id,
        "user_label": hit.user_label,
        "created_at": hit.created_at,
        "created_at_iso": iso_local(hit.created_at, tz_name),
        "word_count": hit.word_count,
        "snippet": hit.snippet,
        "source": "server",
    }
