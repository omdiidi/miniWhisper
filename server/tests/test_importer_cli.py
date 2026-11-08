"""Part 7 Task 5: ``python -m wispralt.importer`` exit codes against a temp DB + FakeSource."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from wispralt._errors import ExportBlocked, ExportUnavailable
from wispralt.importer import __main__ as cli
from wispralt.importer import supabase_export as sx

from ._import_fakes import FakeSource

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import MakeUser

    from wispralt.database import Database


class _Blocked(FakeSource):
    def columns(self, table: str) -> list[str]:
        raise ExportBlocked("project is paused")


@pytest.fixture
def snap(tmp_path: Path) -> Path:
    return sx.export_snapshot(FakeSource(), tmp_path / "backups", date(2026, 11, 7))


@pytest.fixture
def admin(make_user: MakeUser) -> int:
    return make_user(role="admin")[0].id


def _count(db: Database, sql: str) -> int:
    with db.read() as con:
        return con.execute(sql).fetchone()[0]


def test_export_ok_blocked_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "backups"
    monkeypatch.setattr("wispralt.importer.PsqlSource", FakeSource)
    assert cli.main(["export", "--backups-root", str(root)]) == cli.OK
    assert "supabase-snapshot-" in capsys.readouterr().out
    monkeypatch.setattr("wispralt.importer.PsqlSource", _Blocked)
    assert cli.main(["export", "--backups-root", str(root)]) == cli.BLOCKED
    blocked = [d for d in root.iterdir() if (d / "BLOCKED.json").exists()]
    assert len(blocked) == 1 and len(list(root.iterdir())) == 2  # the first snapshot is kept

    def unavailable() -> FakeSource:
        raise ExportUnavailable("no WISPRALT_IMPORT_PG_URL")

    monkeypatch.setattr("wispralt.importer.PsqlSource", unavailable)
    assert cli.main(["export", "--backups-root", str(root)]) == cli.ERROR


def test_snapshot_dry_run_then_yes(
    db: Database, snap: Path, admin: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["snapshot", str(snap), "--dry-run"]) == cli.OK
    out = capsys.readouterr().out
    assert "DRY RUN" in out and f"-> local admin {admin}" in out and "No dictation text" in out
    assert _count(db, "SELECT count(*) FROM users") == 1
    assert cli.main(["snapshot", str(snap), "--yes"]) == cli.OK
    assert _count(db, "SELECT count(*) FROM users") == 3
    assert _count(db, "SELECT count(*) FROM usage_events WHERE source IS NOT NULL") == 13


def test_snapshot_refusals(
    db: Database, snap: Path, admin: int, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["snapshot", str(snap)]) == cli.REFUSED  # non-TTY, no --yes
    assert cli.main(["snapshot", str(snap), "--owner-map", "1=x", "--yes"]) == cli.REFUSED
    make_user(role="admin")
    assert cli.main(["snapshot", str(snap), "--yes"]) == cli.REFUSED  # ambiguous owner
    assert cli.main(["snapshot", str(snap), "--yes", "--owner-map", f"1={admin}"]) == cli.OK


def test_snapshot_interactive_prompt(
    db: Database, snap: Path, admin: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert cli.main(["snapshot", str(snap)]) == cli.REFUSED
    assert _count(db, "SELECT count(*) FROM users") == 1
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert cli.main(["snapshot", str(snap)]) == cli.OK
    assert _count(db, "SELECT count(*) FROM users") == 3


def test_snapshot_integrity_and_blocked_codes(
    db: Database, snap: Path, admin: int, tmp_path: Path
) -> None:
    f = snap / "users.jsonl"
    f.write_text(f.read_text() + "\n")
    assert cli.main(["snapshot", str(snap), "--yes"]) == cli.INTEGRITY
    d = tmp_path / "blocked"
    d.mkdir()
    sx.write_json_new(d / "BLOCKED.json", {"reason": "paused"})
    assert cli.main(["snapshot", str(d), "--dry-run"]) == cli.BLOCKED


def test_files_command(
    db: Database, tmp_path: Path, admin: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "meetings"
    d.mkdir()
    (d / "m.json").write_text(json.dumps({"job_id": "j1", "segments": [{"text": "hi"}]}))
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert cli.main(["files", str(d), "--owner", str(admin)]) == cli.REFUSED
    assert cli.main(["files", str(d), "--owner", str(admin), "--dry-run"]) == cli.OK
    assert _count(db, "SELECT count(*) FROM jobs") == 0
    assert cli.main(["files", str(d), "--owner", str(admin), "--yes"]) == cli.OK
    assert _count(db, "SELECT count(*) FROM jobs WHERE source = 'files'") == 1
    assert cli.main(["files", str(d), "--owner", "999", "--yes"]) == cli.REFUSED


def test_real_run_takes_the_lock_before_migrating_and_dry_run_never_migrates(
    db: Database, tmp_path: Path, admin: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: a live server's DB is never migrated; a dry run never migrates at all."""
    from wispralt.database import Database as Db
    from wispralt.ops.backup import server_lock

    d = tmp_path / "meetings"
    d.mkdir()
    (d / "m.json").write_text(json.dumps({"job_id": "j1", "segments": [{"text": "hi"}]}))
    migrated: list[int] = []
    real = Db.migrate
    monkeypatch.setattr(Db, "migrate", lambda self: migrated.append(1) or real(self))
    assert cli.main(["files", str(d), "--owner", str(admin), "--dry-run"]) == cli.OK
    assert migrated == []
    with server_lock(db.path.parent):  # a running server holds it
        assert cli.main(["files", str(d), "--owner", str(admin), "--yes"]) == cli.REFUSED
    assert migrated == [] and _count(db, "SELECT count(*) FROM jobs") == 0
    assert cli.main(["files", str(d), "--owner", str(admin), "--yes"]) == cli.OK  # lock nests
    assert migrated == [1] and _count(db, "SELECT count(*) FROM jobs") == 1


def test_missing_db_points_at_the_wispralt_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["files", str(tmp_path), "--owner", "1", "--dry-run"]) == cli.ERROR
    err = capsys.readouterr().err
    assert "wispralt setup --role host" in err and "wispralt.bootstrap" not in err
