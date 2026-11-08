"""
importer/runs.py — ``ImportRuns``: the ``import_runs`` repository.

One row per (source, run). A real run commits each batch together with
``checkpoint(con, …)`` (same transaction), so a crashed run is resumed by
``begin`` — same row, its checkpoint and its accumulated counts. Dry runs never
touch this table. "First run" for a source = no ``done`` row yet.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from wispralt.database import Database


@dataclass(frozen=True)
class Run:
    id: int
    checkpoint: str | None
    counts: dict[str, int]


class ImportRuns:
    def __init__(self, db: Database) -> None:
        self.db = db

    def has_done(self, source: str) -> bool:
        with self.db.read() as con:
            return bool(
                con.execute(
                    "SELECT 1 FROM import_runs WHERE source = ? AND status = 'done' LIMIT 1",
                    (source,),
                ).fetchone()
            )

    def begin(self, source: str, origin: str) -> Run:
        """Resume the latest unfinished run of ``source`` from ``origin``, else start one."""
        with self.db.write() as con:
            row = con.execute(
                "SELECT id, checkpoint, counts_json FROM import_runs WHERE source = ? AND origin = ?"
                " AND status IN ('running','failed') AND dry_run = 0 ORDER BY id DESC LIMIT 1",
                (source, origin),
            ).fetchone()
            if row:
                con.execute("UPDATE import_runs SET status = 'running' WHERE id = ?", (row["id"],))
                return Run(row["id"], row["checkpoint"], json.loads(row["counts_json"] or "{}"))
            cur = con.execute(
                "INSERT INTO import_runs(source, origin, started_at, status, dry_run)"
                " VALUES (?, ?, ?, 'running', 0)",
                (source, origin, time.time()),
            )
            return Run(int(cur.lastrowid or 0), None, {})

    @staticmethod
    def checkpoint(
        con: sqlite3.Connection, run_id: int, checkpoint: str, counts: dict[str, int]
    ) -> None:
        """Record progress inside the caller's batch transaction."""
        con.execute(
            "UPDATE import_runs SET checkpoint = ?, counts_json = ? WHERE id = ?",
            (checkpoint, json.dumps(counts, sort_keys=True), run_id),
        )

    def finish(self, run_id: int, status: str, counts: dict[str, int]) -> None:
        with self.db.write() as con:
            con.execute(
                "UPDATE import_runs SET status = ?, finished_at = ?, counts_json = ? WHERE id = ?",
                (status, time.time(), json.dumps(counts, sort_keys=True), run_id),
            )
