"""Task 5: backup / restore commands wrap Part 4's offline functions in service_stopped."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from wispralt.accounts import service
from wispralt.accounts.store import AccountStore
from wispralt.cli import host, launchd, ops
from wispralt.database import Database

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.paths import Ctx

    from .conftest import CliFn, FakeRunner


@pytest.fixture
def running(ctx: Ctx, runner: FakeRunner) -> Ctx:
    host.bootstrap_admin(ctx.paths.data_dir, "owner")
    accounts = AccountStore(Database(ctx.paths.db_path))
    for i in range(3):
        service.create_user(accounts, label=f"u{i}", role="employee", kind="employee")
    launchd.start(ctx)
    runner.calls.clear()
    return ctx


def _count_users(ctx: Ctx) -> int:
    with Database(ctx.paths.db_path).read() as con:
        return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def test_backup_stops_then_restarts(running: Ctx, cli: CliFn, runner: FakeRunner) -> None:
    code, out, _ = cli("backup")
    assert code == 0 and "4 users" in out
    assert [v for v in runner.verbs() if v in {"bootout", "bootstrap"}] == ["bootout", "bootstrap"]
    [bk] = ops.list_backups(running)
    assert bk.parent == running.paths.backup_dir and (bk / "manifest.json").is_file()


def test_backup_failure_still_restarts_and_exits_1(
    running: Ctx, cli: CliFn, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wispralt._errors import InsufficientSpace

    def _full(*_a: object) -> None:
        raise InsufficientSpace("need 9 GB")

    monkeypatch.setattr(ops.offline, "backup", _full)
    code, _, err = cli("backup")
    assert code == 1 and "InsufficientSpace" in err
    assert runner.verbs().count("bootstrap") == 1


def test_not_running_is_not_started(ctx: Ctx, cli: CliFn, runner: FakeRunner) -> None:
    host.bootstrap_admin(ctx.paths.data_dir, "owner")
    assert cli("backup")[0] == 0
    assert "bootstrap" not in runner.verbs() and "bootout" not in runner.verbs()


def test_dest_inside_data_dir_is_refused(running: Ctx, cli: CliFn, runner: FakeRunner) -> None:
    code, _, err = cli("backup", "--dest", str(running.paths.data_dir / "bk"))
    assert code == 1 and "overlaps" in err
    assert runner.verbs().count("bootstrap") == 1
    assert not (running.paths.data_dir / "bk").exists()


def test_round_trip_and_pre_restore_in_status(running: Ctx, cli: CliFn, tmp_path: Path) -> None:
    dest = tmp_path / "bk1"
    assert cli("backup", "--dest", str(dest))[0] == 0
    before = _count_users(running)
    service.create_user(
        AccountStore(Database(running.paths.db_path)),
        label="late",
        role="employee",
        kind="employee",
    )
    assert _count_users(running) == before + 1

    code, _, err = cli("restore", str(dest))
    assert code == 2 and "--yes" in err  # non-interactive needs --yes; nothing touched
    assert _count_users(running) == before + 1

    code, out, _ = cli("restore", str(dest), "--yes")
    assert code == 0 and _count_users(running) == before
    [aside] = ops.pre_restore_dirs(running)
    assert f"previous data kept at {aside}" in out
    status = json.loads(cli("status", "--json")[1])
    assert status["sizes"]["pre_restore"][0]["path"] == str(aside)


def test_restore_rejects_a_non_backup(running: Ctx, cli: CliFn, tmp_path: Path) -> None:
    code, _, err = cli("restore", str(tmp_path), "--yes")
    assert code == 2 and "not a WisprAlt backup" in err


def test_backup_list(running: Ctx, cli: CliFn) -> None:
    assert "no backups" in cli("backup", "list")[1]
    cli("backup")
    rows = json.loads(cli("backup", "list", "--json")[1])
    assert len(rows) == 1 and rows[0]["users"] == 4


def test_client_only_install_has_nothing_to_back_up(ctx: Ctx, cli: CliFn) -> None:
    assert ctx.state is not None
    ctx.state.role = "client"
    ctx.state.save(ctx.paths.state_file)
    code, _, err = cli("backup")
    assert code == 1 and "no WisprAlt host" in err
