"""
usage/store.py — ``UsageStore``: per-request ``usage_events`` rows and the portal reads.

``record`` is one INSERT in its own ``BEGIN IMMEDIATE`` (an unknown user is a
``sqlite3.IntegrityError``). The reads serve the portal overview, the user
detail page, the usage drilldown and its CSV export: windows are relative to the ``now`` passed in, the
p50 is the median of the window's ``duration_ms`` values (computed in Python,
like ``percentile_cont(0.5)``), and the daily buckets are UTC days.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

    from wispralt.database import Database
    from wispralt.usage.events import UsageEvent

_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class UsageFilters:
    """Drilldown / CSV filters (mirrors the /admin/usage query params).

    Naive datetimes are taken as UTC.
    """

    kind: str | None = None
    status: int | None = None
    user_id: int | None = None
    since: datetime | None = None
    until: datetime | None = None

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for clause, value in (
            ("e.kind = ?", self.kind or None),
            ("e.status = ?", self.status),
            ("e.user_id = ?", self.user_id),
            ("e.ts >= ?", _ts(self.since)),
            ("e.ts < ?", _ts(self.until)),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        return (" AND ".join(clauses) or "1"), params


def _ts(value: datetime | None) -> float | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


def _event(r: sqlite3.Row) -> dict[str, Any]:
    row = dict(r)
    row["ts"] = datetime.fromtimestamp(row["ts"], tz=UTC)
    return row


def _median(con: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> float | None:
    values = [
        r[0]
        for r in con.execute(
            f"SELECT duration_ms FROM usage_events WHERE duration_ms IS NOT NULL AND {where}",
            params,
        )
    ]
    return statistics.median(values) if values else None


def _window_counts(
    con: sqlite3.Connection, now: float, user_sql: str, user_params: tuple[Any, ...]
) -> dict[str, Any]:
    r = con.execute(
        "SELECT"
        "  COALESCE(SUM(ts >= ?), 0) AS dictations_24h,"
        "  COALESCE(SUM(ts >= ?), 0) AS dictations_7d,"
        "  COUNT(*) AS dictations_30d,"
        "  COALESCE(SUM(ts >= ? AND status >= 400), 0) AS errors_24h"
        f" FROM usage_events WHERE ts >= ?{user_sql}",
        (now - _DAY, now - 7 * _DAY, now - _DAY, now - 30 * _DAY, *user_params),
    ).fetchone()
    totals = dict(r)
    totals["p50_24h"] = _median(con, f"ts >= ?{user_sql}", (now - _DAY, *user_params))
    return totals


_EVENT_COLUMNS = (
    "e.id, e.user_id, u.label AS user_label, e.ts, e.kind, e.status, e.duration_ms,"
    " e.chars, e.bytes_in, e.bytes_out, e.error_class, e.request_id"
)


class UsageStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, event: UsageEvent) -> None:
        with self.db.write() as con:
            con.execute(
                "INSERT INTO usage_events(user_id, ts, kind, status, chars, duration_ms,"
                " bytes_in, bytes_out, error_class, request_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.user_id,
                    event.ts,
                    event.kind,
                    event.status,
                    event.chars,
                    event.duration_ms,
                    event.bytes_in,
                    event.bytes_out,
                    event.error_class,
                    event.request_id,
                ),
            )

    def overview(self, now: float) -> dict[str, Any]:
        """Portal overview: ``totals``, ``top_users`` (7 d, top 5) and ``daily`` (14 d)."""
        with self.db.read() as con:
            totals = _window_counts(con, now, "", ())
            users = con.execute(
                "SELECT COUNT(*) AS total_users,"
                " COALESCE(SUM(disabled_at IS NULL), 0) AS active_users FROM users"
            ).fetchone()
            totals.update(dict(users))
            top_users = [
                dict(r)
                for r in con.execute(
                    "SELECT u.id, u.label, u.role, COUNT(e.id) AS n"
                    "  FROM users u LEFT JOIN usage_events e ON e.user_id = u.id AND e.ts >= ?"
                    " GROUP BY u.id ORDER BY n DESC, u.id LIMIT 5",
                    (now - 7 * _DAY,),
                )
            ]
            daily = [
                dict(r)
                for r in con.execute(
                    "SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS day, COUNT(*) AS n"
                    "  FROM usage_events WHERE ts >= ? GROUP BY day ORDER BY day",
                    (now - 14 * _DAY,),
                )
            ]
        return {"totals": totals, "top_users": top_users, "daily": daily}

    def user_totals(self, user_id: int, now: float) -> dict[str, Any]:
        """User-detail tiles: 24 h / 7 d / 30 d counts, 24 h errors and p50."""
        with self.db.read() as con:
            return _window_counts(con, now, " AND user_id = ?", (user_id,))

    def recent_events(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as con:
            rows = con.execute(
                "SELECT ts, kind, status, duration_ms, bytes_in, error_class, request_id"
                "  FROM usage_events WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (user_id, limit),
            )
            return [_event(r) for r in rows]

    def events(self, filters: UsageFilters, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        """Drilldown page rows, newest first (with the user's label)."""
        where, params = filters.where()
        with self.db.read() as con:
            rows = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM usage_events e LEFT JOIN users u ON u.id = e.user_id"
                f" WHERE {where} ORDER BY e.ts DESC, e.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [_event(r) for r in rows]

    def iter_events(self, filters: UsageFilters, limit: int = 10_000) -> Iterator[dict[str, Any]]:
        """CSV rows, newest first, capped at *limit*. Consume it in one thread."""
        where, params = filters.where()
        with self.db.read() as con:
            for r in con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM usage_events e LEFT JOIN users u ON u.id = e.user_id"
                f" WHERE {where} ORDER BY e.ts DESC, e.id DESC LIMIT ?",
                (*params, limit),
            ):
                yield _event(r)
