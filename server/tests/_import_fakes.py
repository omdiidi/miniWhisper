"""Shared fakes for the Part 7 importer tests: an in-memory Supabase ``RowSource``."""

from __future__ import annotations

from typing import Any

USER_COLS = [
    "id", "label", "token_hash", "role", "created_at", "last_seen_at", "revoked_at", "notes",
    "display_name", "kind",
]  # fmt: skip


def _ts(day: int, sec: int = 0) -> str:
    return f"2026-04-{day:02d}T10:00:{sec:02d}.250000+00:00"


def supabase_rows() -> dict[str, list[dict[str, Any]]]:
    """3 users (admin, revoked, employee), 10 usage (one ``meeting``, one orphan), 4 fallback."""
    users = [
        {"id": 1, "label": "owner", "token_hash": "h1", "role": "admin", "created_at": _ts(1),
         "last_seen_at": _ts(9), "revoked_at": None, "notes": None, "display_name": "Dana",
         "kind": "employee"},
        {"id": 2, "label": "gone", "token_hash": "h2", "role": "employee", "created_at": _ts(2),
         "last_seen_at": None, "revoked_at": _ts(5), "notes": "left", "display_name": None,
         "kind": "employee"},
        {"id": 3, "label": "zapier", "token_hash": "h3", "role": "employee", "created_at": _ts(3),
         "last_seen_at": None, "revoked_at": None, "notes": None, "display_name": None,
         "kind": "integration"},
    ]  # fmt: skip
    usage = [
        {"id": 100 + i, "user_id": (1, 2, 3)[i % 3], "ts": _ts(10, i), "kind": "dictate",
         "status": 200, "chars": 10 * i, "duration_ms": 1.5 * i, "bytes_in": 1000, "bytes_out": 50,
         "error_class": None, "request_id": f"r{i}"}
        for i in range(10)
    ]  # fmt: skip
    usage[4]["kind"] = "meeting"
    usage[7]["user_id"] = 99  # orphan: no such user
    fallback = [
        {"id": 200 + i, "user_id": 1, "ts": _ts(11, i), "kind": "dictate", "status": 200,
         "bytes_in": 900, "duration_ms": 800.0, "error_class": None, "request_id": f"f{i}",
         "provider": "openai", "provider_request_id": f"p{i}", "cost_micro_usd": 12}
        for i in range(4)
    ]  # fmt: skip
    return {"users": users, "usage_events": usage, "fallback_events": fallback}


class FakeSource:
    """``RowSource`` over in-memory rows; records every call."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows = rows if rows is not None else supabase_rows()
        self.calls: list[tuple[str, str]] = []

    def columns(self, table: str) -> list[str]:
        self.calls.append(("columns", table))
        rows = self.rows.get(table, [])
        return list(rows[0]) if rows else []

    def count(self, table: str) -> tuple[int, int | None]:
        self.calls.append(("count", table))
        rows = self.rows[table]
        return len(rows), max((r["id"] for r in rows), default=None)

    def page(self, table: str, cols: list[str], after_id: int, limit: int) -> list[dict]:
        self.calls.append(("page", table))
        got = sorted((r for r in self.rows[table] if r["id"] > after_id), key=lambda r: r["id"])
        return [{c: r[c] for c in cols} for r in got[:limit]]
