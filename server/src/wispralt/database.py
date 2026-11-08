"""
database.py — the one SQLite database of a Local-mode host (``data_dir/wispralt.db``).

* A short-lived connection per call, each with ``foreign_keys=ON``,
  ``busy_timeout=5000`` and ``synchronous=NORMAL``. WAL is set once, by ``migrate``.
* ``read()`` runs a ``BEGIN`` snapshot; ``write()`` runs ``BEGIN IMMEDIATE`` and
  commits on exit, rolling back if the body raises. Never hold either across
  inference. Async callers wrap repository methods in ``asyncio.to_thread``.
* Numbered migrations ``migrations/NNNN_*.sql`` are applied in one
  ``BEGIN IMMEDIATE`` transaction, statement by statement (``executescript``
  would commit implicitly), and recorded with their sha256 in ``schema_migrations``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from wispralt._errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CREATE_SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS schema_migrations("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
)


def _migration_files() -> dict[int, Path]:
    return {int(f.name[:4]): f for f in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements (comments may contain ``;``)."""
    statements: list[str] = []
    buf = ""
    for part in sql.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            code = "\n".join(ln for ln in buf.splitlines() if not ln.lstrip().startswith("--"))
            if code.strip(" \t\r\n;"):
                statements.append(buf)
            buf = ""
    return statements


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()

    def _connect(self) -> sqlite3.Connection:
        # mode=rw: never create a stray empty DB (migrate creates the file itself).
        con = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5.0
        )
        con.row_factory = sqlite3.Row
        for pragma in ("foreign_keys=ON", "busy_timeout=5000", "synchronous=NORMAL"):
            con.execute(f"PRAGMA {pragma}")
        return con

    @contextmanager
    def _transaction(self, begin: str) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute(begin)
            yield con
            con.execute("COMMIT")
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Snapshot read transaction."""
        with self._transaction("BEGIN") as con:
            yield con

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (``BEGIN IMMEDIATE``); commits on exit, rolls back on raise."""
        with self._transaction("BEGIN IMMEDIATE") as con:
            yield con

    def migrate(self) -> list[int]:
        """Create the DB (0600) if absent and apply pending migrations. Returns versions applied."""
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")  # must run outside a transaction
        files = _migration_files()
        applied_now: list[int] = []
        with self.write() as con:
            con.execute(_CREATE_SCHEMA_MIGRATIONS)
            applied = {
                r["version"]: r["checksum"]
                for r in con.execute("SELECT version, checksum FROM schema_migrations")
            }
            if applied and max(applied) > max(files, default=0):
                raise MigrationError("database schema is newer than this code")
            missing = applied.keys() - files.keys()
            if missing:
                raise MigrationError(f"applied migration file missing: {sorted(missing)}")
            edited = [v for v in applied if applied[v] != _sha256(files[v].read_text())]
            if edited:
                raise MigrationError(f"applied migration file edited: {sorted(edited)}")
            for version in sorted(files.keys() - applied.keys()):
                sql = files[version].read_text()
                for stmt in split_statements(sql):
                    try:
                        con.execute(stmt)
                    except sqlite3.OperationalError as exc:
                        if "no such module: fts5" in str(exc):
                            raise MigrationError("SQLite lacks FTS5") from exc
                        raise
                con.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                    (version, files[version].name, _sha256(sql), time.time()),
                )
                applied_now.append(version)
        return applied_now

    def check(self) -> str | None:
        """Readiness: None when healthy, else ``"db_error"`` or ``"schema_outdated"``."""
        try:
            with self.read() as con:
                applied = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
        except sqlite3.Error:
            return "db_error"
        return None if applied >= _migration_files().keys() else "schema_outdated"
