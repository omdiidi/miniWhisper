"""Database core: migrations, per-connection pragmas, transactions, data_dir validation."""

from __future__ import annotations

import shutil
import sqlite3
import stat
import threading
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from wispralt import database as database_mod
from wispralt._errors import MigrationError
from wispralt.config import Settings
from wispralt.database import Database, split_statements

if TYPE_CHECKING:
    from pathlib import Path

_TABLES = {
    "settings",
    "users",
    "api_keys",
    "invites",
    "usage_events",
    "dictations",
    "jobs",
    "weekly_insights",
    "schema_migrations",
}


@pytest.fixture
def migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the migrations dir that a test may extend or edit."""
    copy = tmp_path / "migrations"
    shutil.copytree(database_mod.MIGRATIONS_DIR, copy)
    monkeypatch.setattr(database_mod, "MIGRATIONS_DIR", copy)
    return copy


def _tables(db: Database) -> set[str]:
    with db.read() as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _versions(db: Database) -> list[int]:
    with db.read() as con:
        return [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY 1")]


def test_fresh_migrate_creates_schema_and_seeds_retention(tmp_path: Path) -> None:
    db = Database(tmp_path / "wispralt.db")
    assert db.migrate() == [1]
    assert _tables(db) == _TABLES
    with db.read() as con:
        fks = {
            table: {
                (r["table"], r["from"]) for r in con.execute(f"PRAGMA foreign_key_list({table})")
            }
            for table in (
                "api_keys",
                "invites",
                "usage_events",
                "dictations",
                "jobs",
                "weekly_insights",
            )
        }
        retention = con.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()[
            0
        ]
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
    assert fks["api_keys"] == {("users", "user_id")}
    assert fks["invites"] == {
        ("users", "user_id"),
        ("users", "created_by"),
        ("api_keys", "api_key_id"),
    }
    for table in ("usage_events", "dictations", "jobs", "weekly_insights"):
        assert fks[table] == {("users", "user_id")}
    assert retention == "0"
    assert journal == "wal"
    assert db.check() is None


def test_db_file_is_0600_before_first_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wispralt.db"
    modes: list[int] = []
    real_connect = sqlite3.connect

    def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
        modes.append(stat.S_IMODE(path.stat().st_mode))
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(database_mod.sqlite3, "connect", spy)
    Database(path).migrate()
    assert modes and set(modes) == {0o600}


def test_second_migrate_is_noop(db: Database) -> None:
    assert db.migrate() == []
    assert _versions(db) == [1]


def test_failing_migration_leaves_nothing(tmp_path: Path, migrations: Path) -> None:
    (migrations / "0002_bad.sql").write_text(
        "CREATE TABLE partial_table(x INTEGER);\nCREATE TABLE broken(;\n"
    )
    db = Database(tmp_path / "wispralt.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()
    with sqlite3.connect(db.path) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "partial_table" not in tables
    assert "schema_migrations" not in tables  # the whole run, incl. 0001, rolled back


def test_failing_second_migration_keeps_first(tmp_path: Path, migrations: Path) -> None:
    db = Database(tmp_path / "wispralt.db")
    db.migrate()
    (migrations / "0002_bad.sql").write_text(
        "CREATE TABLE partial_table(x);\nCREATE TABLE broken(;\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()
    assert "partial_table" not in _tables(db)
    assert _versions(db) == [1]


def test_edited_applied_file_refuses_and_applies_nothing(tmp_path: Path, migrations: Path) -> None:
    db = Database(tmp_path / "wispralt.db")
    db.migrate()
    first = migrations / "0001_initial.sql"
    first.write_text(first.read_text() + "\n-- edited\n")
    (migrations / "0002_next.sql").write_text("CREATE TABLE next_table(x);\n")
    with pytest.raises(MigrationError, match="edited"):
        db.migrate()
    assert "next_table" not in _tables(db)
    assert _versions(db) == [1]


def test_missing_applied_file_is_migration_error(tmp_path: Path, migrations: Path) -> None:
    (migrations / "0002_next.sql").write_text("CREATE TABLE next_table(x);\n")
    db = Database(tmp_path / "wispralt.db")
    assert db.migrate() == [1, 2]
    (migrations / "0001_initial.sql").unlink()
    with pytest.raises(MigrationError, match="missing"):
        db.migrate()


def test_db_newer_than_code_is_migration_error(db: Database) -> None:
    with db.write() as con:
        con.execute("INSERT INTO schema_migrations VALUES (99, '0099_future.sql', 'x', 0)")
    with pytest.raises(MigrationError, match="newer"):
        db.migrate()


def test_pragmas_per_connection_and_fk_enforced(db: Database) -> None:
    for tx in (db.read, db.write):
        with tx() as con:
            assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    with pytest.raises(sqlite3.IntegrityError), db.write() as con:
        con.execute("INSERT INTO api_keys(user_id, token_hash, created_at) VALUES (999, 'h', 0)")


def test_raising_write_rolls_back(db: Database) -> None:
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), db.write() as con:
        con.execute("INSERT INTO settings VALUES ('k', 'v')")
        raise Boom
    with db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM settings WHERE key='k'").fetchone()[0] == 0


def test_concurrent_writers_both_commit(db: Database) -> None:
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(key: str) -> None:
        try:
            barrier.wait()
            with db.write() as con:
                con.execute("INSERT INTO settings VALUES (?, 'v')", (key,))
        except sqlite3.Error as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 't%'").fetchone()[0] == 2


def test_check_reports_outdated_and_db_error(db: Database, tmp_path: Path) -> None:
    with db.write() as con:
        con.execute("DELETE FROM schema_migrations")
    assert db.check() == "schema_outdated"
    assert Database(tmp_path / "absent.db").check() == "db_error"
    assert not (tmp_path / "absent.db").exists()  # check never creates a DB


def test_split_statements_ignores_semicolons_in_comments() -> None:
    sql = "-- a; b\nCREATE TABLE t(x);\n\nINSERT INTO t VALUES (';');  -- c\n"
    assert [s.strip() for s in split_statements(sql)] == [
        "-- a; b\nCREATE TABLE t(x);",
        "INSERT INTO t VALUES (';');",
    ]


@pytest.mark.parametrize("value", ["", "rel/dir"])
def test_data_dir_must_be_absolute(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=value)  # type: ignore[call-arg]


def test_data_dir_derives_db_and_outputs(tmp_path: Path) -> None:
    s = Settings(_env_file=None, data_dir=str(tmp_path))  # type: ignore[call-arg]
    assert s.db_path == tmp_path / "wispralt.db"
    assert s.outputs_dir == tmp_path / "outputs"
