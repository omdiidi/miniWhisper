"""
imports.py — the generic, idempotent history writer (Part 7).

``insert_rows(con, table, rows, source)`` runs inside a caller's ``Database.write()``
so an importer can commit a batch and its ``import_runs`` checkpoint together;
``import_rows(db, …)`` is the one-transaction wrapper. Each row is a dict of
allowlisted columns plus ``source_id``. Every row lands in exactly one bucket:

* inserted   — ``ON CONFLICT(source, source_id) WHERE source IS NOT NULL DO NOTHING``
  returned an id;
* duplicate  — the ``(source, source_id)`` already existed (its id is read in the
  same transaction, so ``ids`` is identical on a re-run);
* error      — another constraint failed (FK, ``client_dedup_id``, CHECK); the row's
  SAVEPOINT is rolled back, the rest of the batch still commits.

Validation (table, columns, ``source_id``) happens before any write and raises
``ValueError``. ``api_keys`` and ``invites`` are never importable: credentials
are not history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wispralt._errors import ImportIntegrityError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from wispralt.database import Database

COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset(
        {"label", "display_name", "notes", "role", "kind", "created_at", "disabled_at"}
    ),
    "usage_events": frozenset(
        {
            "user_id",
            "ts",
            "kind",
            "status",
            "chars",
            "duration_ms",
            "bytes_in",
            "bytes_out",
            "error_class",
            "request_id",
        }
    ),
    "dictations": frozenset(
        {
            "user_id",
            "created_at",
            "duration_ms",
            "text",
            "word_count",
            "client_app_version",
            "client_dedup_id",
            "deleted_at",
        }
    ),
    "jobs": frozenset(
        {
            "id",
            "user_id",
            "status",
            "mode",
            "created_at",
            "started_at",
            "finished_at",
            "error",
            "wav_path",
            "audio_duration_s",
            "transcript_text",
            "word_count",
            "client_app_version",
            "estimated",
        }
    ),
}


@dataclass(frozen=True)
class RowError:
    source_id: str
    error: str  # the SQLite constraint message


@dataclass
class ImportResult:
    inserted: int = 0
    duplicates: int = 0
    ids: dict[str, int | str] = field(default_factory=dict)  # source_id -> local id
    errors: list[RowError] = field(default_factory=list)


def _validate(table: str, rows: Sequence[dict[str, Any]], source: str) -> None:
    if table not in COLUMNS:
        raise ValueError(f"table not importable: {table!r}")
    if not source:
        raise ValueError("source is required")
    allowed = COLUMNS[table] | {"source_id"}
    for row in rows:
        sid = row.get("source_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError(f"{table}: every row needs a non-empty str source_id")
        extra = row.keys() - allowed
        if extra:
            raise ValueError(f"{table}: unknown columns {sorted(extra)}")


def insert_rows(
    con: sqlite3.Connection, table: str, rows: Sequence[dict[str, Any]], source: str
) -> ImportResult:
    """Insert ``rows`` inside the caller's write transaction (see module docstring)."""
    _validate(table, rows, source)
    result = ImportResult()
    for i, row in enumerate(rows):
        sid = row["source_id"]
        cols = [c for c in row if c != "source_id"]
        names = ", ".join([*cols, "source", "source_id"])  # identifiers from COLUMNS only
        marks = ", ".join("?" * (len(cols) + 2))
        con.execute(f"SAVEPOINT sp_{i}")
        try:
            got = con.execute(
                f"INSERT INTO {table}({names}) VALUES ({marks})"
                " ON CONFLICT(source, source_id) WHERE source IS NOT NULL DO NOTHING RETURNING id",
                [*(row[c] for c in cols), source, sid],
            ).fetchall()
        except sqlite3.IntegrityError as exc:
            con.execute(f"ROLLBACK TO sp_{i}")
            con.execute(f"RELEASE sp_{i}")
            result.errors.append(RowError(sid, str(exc)))
            continue
        con.execute(f"RELEASE sp_{i}")
        if got:
            result.inserted += 1
            result.ids[sid] = got[0][0]
        else:
            result.duplicates += 1
            result.ids[sid] = con.execute(
                f"SELECT id FROM {table} WHERE source = ? AND source_id = ?", (source, sid)
            ).fetchone()[0]
    return result


def import_rows(
    db: Database, table: str, rows: Sequence[dict[str, Any]], source: str
) -> ImportResult:
    """``insert_rows`` in its own write transaction; validation runs before it opens."""
    _validate(table, rows, source)
    with db.write() as con:
        return insert_rows(con, table, rows, source)


def link_existing(
    con: sqlite3.Connection, table: str, local_id: int | str, source: str, source_id: str
) -> bool:
    """Stamp an existing local row with ``(source, source_id)``. True if newly linked.

    Idempotent for the same pair; ``ImportIntegrityError`` when the row is missing,
    already linked to something else, or the pair belongs to another row.
    """
    if table not in COLUMNS:
        raise ValueError(f"table not importable: {table!r}")
    try:
        cur = con.execute(
            f"UPDATE {table} SET source = ?, source_id = ? WHERE id = ? AND source IS NULL",
            (source, source_id, local_id),
        )
    except sqlite3.IntegrityError as exc:
        raise ImportIntegrityError(f"{source}:{source_id} already belongs to another row") from exc
    if cur.rowcount:
        return True
    row = con.execute(f"SELECT source, source_id FROM {table} WHERE id = ?", (local_id,)).fetchone()
    if row is None:
        raise ImportIntegrityError(f"{table} {local_id} does not exist")
    if (row["source"], row["source_id"]) != (source, source_id):
        raise ImportIntegrityError(
            f"{table} {local_id} is already linked to {row['source']}:{row['source_id']}"
        )
    return False
