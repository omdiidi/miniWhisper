"""Part 7 Task 1: import_runs migration, the generic import_rows writer, link_existing, runs, lock."""

from __future__ import annotations

import fcntl
import os
from typing import TYPE_CHECKING

import pytest

from wispralt._errors import ImportIntegrityError, ServerRunning
from wispralt.importer.lock import offline_lock
from wispralt.importer.runs import ImportRuns
from wispralt.imports import RowError, import_rows, link_existing

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MakeUser

    from wispralt.database import Database


def _count(db: Database, sql: str) -> int:
    with db.read() as con:
        return con.execute(sql).fetchone()[0]


def test_migration_0003_applies_after_0001_with_gap(db: Database) -> None:
    with db.read() as con:
        versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert versions[0] == 1 and 3 in versions
    assert db.migrate() == []  # checksum gate: nothing pending, nothing edited
    assert db.check() is None


def test_same_rows_twice_insert_then_duplicate(db: Database) -> None:
    rows = [
        {"source_id": str(i), "label": f"u{i}", "role": "employee", "created_at": 1.0}
        for i in (1, 2)
    ]
    first = import_rows(db, "users", rows, "supabase.users")
    second = import_rows(db, "users", rows, "supabase.users")
    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert first.ids == second.ids
    assert _count(db, "SELECT count(*) FROM users") == 2


def test_mixed_batch_commits_good_rows_and_reports_errors(
    db: Database, make_user: MakeUser
) -> None:
    user, _ = make_user()
    with db.write() as con:
        con.execute(
            "INSERT INTO dictations(user_id, created_at, text, client_dedup_id) VALUES (?, 1, 'x', 'dup')",
            (user.id,),
        )
    rows = [
        {"source_id": "a", "user_id": user.id, "created_at": 2.0, "text": "ok"},
        {"source_id": "b", "user_id": 9999, "created_at": 2.0, "text": "bad fk"},
        {
            "source_id": "c",
            "user_id": user.id,
            "created_at": 2.0,
            "text": "c",
            "client_dedup_id": "dup",
        },
        {"source_id": "d", "user_id": user.id, "created_at": 3.0, "text": "ok2"},
    ]
    res = import_rows(db, "dictations", rows, "files")
    assert res.inserted == 2 and res.duplicates == 0
    assert [e.source_id for e in res.errors] == ["b", "c"]
    assert all(isinstance(e, RowError) and "constraint" in e.error.lower() for e in res.errors)
    assert _count(db, "SELECT count(*) FROM dictations WHERE source = 'files'") == 2


@pytest.mark.parametrize(
    ("table", "row"),
    [
        ("api_keys", {"source_id": "1"}),
        ("users", {"source_id": "1", "label": "x", "token_hash": "h"}),
        ("users", {"label": "x"}),
        ("users", {"source_id": "", "label": "x"}),
    ],
)
def test_bad_input_raises_before_any_write(db: Database, table: str, row: dict) -> None:
    good = {"source_id": "ok", "label": "ok", "created_at": 1.0}
    with pytest.raises(ValueError):
        import_rows(db, table, [good, row], "s")
    assert _count(db, "SELECT count(*) FROM users") == 0


def test_link_existing_idempotent_and_guarded(db: Database, make_user: MakeUser) -> None:
    admin, _ = make_user(role="admin")
    other, _ = make_user()
    with db.write() as con:
        assert link_existing(con, "users", admin.id, "supabase.users", "7") is True
        assert link_existing(con, "users", admin.id, "supabase.users", "7") is False
    with pytest.raises(ImportIntegrityError), db.write() as con:
        link_existing(con, "users", admin.id, "supabase.users", "8")
    with pytest.raises(ImportIntegrityError), db.write() as con:
        link_existing(con, "users", other.id, "supabase.users", "7")
    with pytest.raises(ImportIntegrityError), db.write() as con:
        link_existing(con, "users", 4242, "supabase.users", "9")


def test_runs_resume_unfinished_and_track_done(db: Database) -> None:
    runs = ImportRuns(db)
    run = runs.begin("supabase.users", "/snap")
    assert run.checkpoint is None and not runs.has_done("supabase.users")
    with db.write() as con:
        ImportRuns.checkpoint(con, run.id, "500", {"new": 500})
    again = runs.begin("supabase.users", "/snap")
    assert (again.id, again.checkpoint, again.counts) == (run.id, "500", {"new": 500})
    runs.finish(run.id, "done", {"new": 600})
    assert runs.has_done("supabase.users")
    assert runs.begin("supabase.users", "/snap").id != run.id


def test_offline_lock_refuses_when_held(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fd = os.open(tmp_path / "data.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ServerRunning), offline_lock(data_dir):
            pass
    finally:
        os.close(fd)
    with offline_lock(data_dir):
        pass  # free again once released
