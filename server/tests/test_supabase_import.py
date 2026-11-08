"""Part 7 Task 3: snapshot import (owner link, FK remap, kind map, accounting, resume, dry run)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from wispralt._errors import ExportBlocked, ImportIntegrityError, OwnerMappingRequired
from wispralt.importer import supabase_import as si
from wispralt.importer.supabase_export import export_snapshot, write_json_new

from ._import_fakes import FakeSource

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MakeUser

    from wispralt.database import Database


@pytest.fixture
def snap(tmp_path: Path) -> Path:
    return export_snapshot(FakeSource(), tmp_path / "backups", date(2026, 11, 7))


@pytest.fixture
def admin_id(make_user: MakeUser) -> int:
    return make_user(role="admin", label="local-owner")[0].id


def _db_hash(db: Database) -> str:
    """DB file plus its WAL (in WAL mode a write may only reach ``-wal``)."""
    wal = db.path.with_name(db.path.name + "-wal")
    data = db.path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    return hashlib.sha256(data).hexdigest()


def _rows(db: Database, sql: str, *args: object) -> list[dict]:
    with db.read() as con:
        return [dict(r) for r in con.execute(sql, args)]


def _balanced(report: si.Report) -> None:
    for c in report.tables.values():
        parts = ("new", "duplicate", "linked", "malformed", "unmatched", "errors")
        assert c["source_count"] == sum(c[k] for k in parts), c


def test_first_import_maps_users_events_and_owner(db: Database, snap: Path, admin_id: int) -> None:
    report = si.import_snapshot(db, snap, confirm=True)
    t = report.tables
    assert (t["users"]["new"], t["users"]["linked"], t["users"]["duplicate"]) == (2, 1, 0)
    assert (t["usage_events"]["new"], t["usage_events"]["unmatched"]) == (9, 1)
    assert t["fallback_events"]["new"] == 4
    _balanced(report)
    users = {u["source_id"]: u for u in _rows(db, "SELECT * FROM users WHERE source IS NOT NULL")}
    assert (
        users["1"]["id"] == admin_id and users["1"]["label"] == "local-owner"
    )  # linked, not renamed
    assert users["2"]["disabled_at"] is not None and users["3"]["kind"] == "integration"
    assert _rows(db, "SELECT count(*) AS n FROM api_keys")[0]["n"] == 1  # no keys imported
    usage = {
        u["source_id"]: u
        for u in _rows(db, "SELECT * FROM usage_events WHERE source = 'supabase.usage_events'")
    }
    assert usage["100"]["user_id"] == admin_id
    assert usage["101"]["user_id"] == users["2"]["id"]
    assert usage["104"]["kind"] == "file"  # meeting → file
    assert usage["100"]["ts"] == datetime(2026, 4, 10, 10, 0, 0, 250000, tzinfo=UTC).timestamp()
    fb = _rows(
        db, "SELECT kind, user_id FROM usage_events WHERE source = 'supabase.fallback_events'"
    )
    assert {(r["kind"], r["user_id"]) for r in fb} == {("fallback_dictate", admin_id)}
    runs = _rows(db, "SELECT source, status, counts_json FROM import_runs ORDER BY id")
    assert [(r["source"], r["status"]) for r in runs] == [
        ("supabase.users", "done"), ("supabase.usage_events", "done"),
        ("supabase.fallback_events", "done"),
    ]  # fmt: skip
    assert json.loads(runs[1]["counts_json"])["inserted"] == 9
    assert si.NO_TEXT in report.render()


def test_rerun_is_idempotent(db: Database, snap: Path, admin_id: int) -> None:
    si.import_snapshot(db, snap, confirm=True)
    again = si.import_snapshot(db, snap)  # earlier link counts as the confirmed owner
    assert all(c["new"] == 0 for c in again.tables.values())
    assert (
        again.tables["users"]["duplicate"] == 2 and again.tables["usage_events"]["duplicate"] == 9
    )
    _balanced(again)


def test_tampered_snapshot_writes_nothing(db: Database, snap: Path, admin_id: int) -> None:
    f = snap / "usage_events.jsonl"
    f.chmod(0o600)
    f.write_text(f.read_text().replace('"status":200', '"status":500', 1))
    before = _db_hash(db)
    with pytest.raises(ImportIntegrityError, match="sha256"):
        si.import_snapshot(db, snap, confirm=True)
    assert _db_hash(db) == before


def test_line_count_mismatch_refused(db: Database, tmp_path: Path, snap: Path) -> None:
    counts = json.loads((snap / "counts.json").read_text())
    counts["users"]["source_count"] = 4
    bad = tmp_path / "bad"
    bad.mkdir()
    for t in ("users", "usage_events", "fallback_events"):
        (bad / f"{t}.jsonl").write_bytes((snap / f"{t}.jsonl").read_bytes())
    write_json_new(bad / "counts.json", counts)
    with pytest.raises(ImportIntegrityError, match="lines"):
        si.import_snapshot(db, bad, dry_run=True)


def test_first_run_duplicate_aborts(db: Database, snap: Path, admin_id: int) -> None:
    with db.write() as con:
        con.execute(
            "INSERT INTO usage_events(user_id, ts, kind, status, source, source_id)"
            " VALUES (?, 1, 'dictate', 200, 'supabase.usage_events', '103')",
            (admin_id,),
        )
    with pytest.raises(ImportIntegrityError, match="first run"):
        si.import_snapshot(db, snap, confirm=True)
    assert _rows(db, "SELECT status FROM import_runs WHERE source = 'supabase.usage_events'") == [
        {"status": "failed"}
    ]
    assert _rows(db, "SELECT count(*) AS n FROM usage_events")[0]["n"] == 1  # batch rolled back


def test_dry_run_writes_nothing_and_shows_proposal(db: Database, snap: Path, admin_id: int) -> None:
    before = _db_hash(db)
    report = si.import_snapshot(db, snap, dry_run=True)
    assert _db_hash(db) == before
    assert report.tables["users"]["new"] == 2 and report.tables["usage_events"]["unmatched"] == 1
    assert f"Supabase user 1 -> local admin {admin_id}" in report.render()
    _balanced(report)


def test_owner_mapping_required(
    db: Database, snap: Path, admin_id: int, make_user: MakeUser
) -> None:
    with pytest.raises(OwnerMappingRequired, match="proposed 1="):
        si.import_snapshot(db, snap)
    make_user(role="admin")  # now ambiguous: --yes alone is not enough
    with pytest.raises(OwnerMappingRequired, match="no unambiguous"):
        si.import_snapshot(db, snap, confirm=True)
    with pytest.raises(OwnerMappingRequired, match="unknown"):
        si.import_snapshot(db, snap, owner_map={42: admin_id})
    report = si.import_snapshot(db, snap, owner_map={1: admin_id})
    assert report.tables["users"]["linked"] == 1


def test_no_local_admin_refuses_until_setup(db: Database, snap: Path) -> None:
    with pytest.raises(OwnerMappingRequired, match="wispralt setup --role host"):
        si.import_snapshot(db, snap, confirm=True)


def test_malformed_user_is_reported_and_its_events_unmatched(
    db: Database, tmp_path: Path, admin_id: int
) -> None:
    src = FakeSource()
    src.rows["users"][2]["kind"] = "robot"
    d = export_snapshot(src, tmp_path / "b", date(2026, 11, 7))
    report = si.import_snapshot(db, d, confirm=True)
    assert report.tables["users"]["malformed"] == 1
    assert report.tables["usage_events"]["unmatched"] == 1 + 3  # orphan + the robot's 3 rows
    assert any("robot" in p for p in report.problems)
    _balanced(report)


def test_crash_after_first_batch_resumes_from_checkpoint(
    db: Database, snap: Path, admin_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si, "BATCH", 4)
    real = si.insert_rows
    calls = {"n": 0}

    def crashing(con, table, rows, source):  # noqa: ANN001, ANN202
        if source == "supabase.usage_events":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("power cut")
        return real(con, table, rows, source)

    monkeypatch.setattr(si, "insert_rows", crashing)
    with pytest.raises(RuntimeError):
        si.import_snapshot(db, snap, confirm=True)
    run = _rows(db, "SELECT * FROM import_runs WHERE source = 'supabase.usage_events'")
    assert run[0]["status"] == "running" and run[0]["checkpoint"] == "103"
    monkeypatch.setattr(si, "insert_rows", real)
    report = si.import_snapshot(db, snap, confirm=True)
    assert (
        report.tables["usage_events"]["new"] == 9
        and report.tables["usage_events"]["duplicate"] == 0
    )
    runs = _rows(db, "SELECT id, status FROM import_runs WHERE source = 'supabase.usage_events'")
    assert runs == [{"id": run[0]["id"], "status": "done"}]  # same run row, resumed
    assert len(_rows(db, "SELECT id FROM usage_events WHERE source = 'supabase.usage_events'")) == 9


def test_blocked_snapshot_raises_blocked(db: Database, tmp_path: Path) -> None:
    d = tmp_path / "supabase-snapshot-2026-11-07"
    d.mkdir()
    write_json_new(d / "BLOCKED.json", {"reason": "paused", "at": "x"})
    with pytest.raises(ExportBlocked, match="paused"):
        si.import_snapshot(db, d, dry_run=True)
