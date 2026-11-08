"""
settings_store.py — ``SettingsStore``: the ``settings`` key/value table (runtime settings).

Holds what an admin changes at runtime (AI insights enablement, cost cap, model,
timezone) and small server counters (``server_starts``). Values are TEXT; callers
convert. Static configuration stays in ``config.Settings`` (``server/.env``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from wispralt.database import Database


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        with self.db.read() as con:
            r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if r is None else r[0]

    def get_all(self) -> dict[str, str]:
        with self.db.read() as con:
            return {r[0]: r[1] for r in con.execute("SELECT key, value FROM settings")}

    def set_many(self, values: Mapping[str, str | None]) -> None:
        """Upsert every pair in one transaction; a ``None`` value deletes the key."""
        with self.db.write() as con:
            for key, value in values.items():
                if value is None:
                    con.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    con.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )

    def incr(self, key: str) -> int:
        """Atomically add 1 to an integer setting (absent → 1). Returns the new value."""
        with self.db.write() as con:
            return int(
                con.execute(
                    "INSERT INTO settings(key, value) VALUES (?, '1')"
                    " ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
                    " RETURNING value",
                    (key,),
                ).fetchone()[0]
            )
