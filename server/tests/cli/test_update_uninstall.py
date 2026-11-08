"""Task 8: update (two phases across an execv) and uninstall (only what state lists)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from wispralt.cli.paths import InstallState, Paths

from .conftest import make_app

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import CliFn, Execs, FakeHttp, FakeHub, FakeRunner


@pytest.fixture
def installed(cli: CliFn, hub: FakeHub, clone: Path, runner: FakeRunner, tmp_path: Path) -> Paths:
    code, _, err = cli(
        "setup", "--role", "host", "--non-interactive", "--source", str(clone),
        "--token-file", str(tmp_path / "admin.key"), "--no-open",
    )  # fmt: skip
    assert code == 0, err
    runner.calls.clear()
    return Paths.resolve()


def _short(call: list[str]) -> str:
    return " ".join(call[:2]) if call[0] != "launchctl" else f"launchctl {call[1]}"


def test_update_order(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs, clone: Path
) -> None:
    assert cli("update")[0] == 0
    steps = [_short(c) for c in runner.calls if c[:2] != ["launchctl", "print"]]
    assert steps == ["git -C", "git -C", "launchctl bootout", "uv tool"]
    assert runner.calls[0][3:] == ["status", "--porcelain"]
    assert runner.calls[1][3:] == ["pull", "--ff-only"]
    assert runner.calls[-1] == [
        "uv", "tool", "install", "--force", "--python", "3.11", "--reinstall", f"{clone}/server[host]",
    ]  # fmt: skip
    [bk] = _backups(installed)
    [argv] = execs.calls
    assert argv[1:] == ["update", "--_phase2", "--_backup", str(bk)]

    runner.calls.clear()
    code, out, _ = cli(*argv[1:])
    assert code == 0 and "healthy" in out
    assert [c[1] for c in runner.calls if c[0] == "launchctl"][-1] == "bootstrap"


def _backups(paths: Paths) -> list[Path]:
    root = paths.backup_dir
    return (
        sorted(p for p in root.iterdir() if (p / "manifest.json").is_file())
        if root.is_dir()
        else []
    )


def test_dirty_tree_is_refused(installed: Paths, cli: CliFn, runner: FakeRunner) -> None:
    runner.on("git", "-C", out=" M server/x.py\n")
    code, _, err = cli("update")
    assert code == 1 and "local changes" in err
    assert not any("pull" in c for c in runner.calls) and "bootout" not in runner.verbs()


def test_no_pull_no_backup(installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs) -> None:
    assert cli("update", "--no-pull", "--no-backup")[0] == 0
    assert not any("pull" in c for c in runner.calls)
    assert _backups(installed) == []
    assert execs.calls[0][1:] == ["update", "--no-pull", "--no-backup", "--_phase2"]


def test_failed_reinstall_restarts_the_old_service(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs
) -> None:
    runner.on("uv", "tool", "install", rc=1, err="resolution failed")
    code, _, err = cli("update")
    assert code == 1 and "resolution failed" in err
    assert runner.verbs()[-1] == "bootstrap" and execs.calls == []


def test_unhealthy_phase2_points_at_the_backup(
    installed: Paths, cli: CliFn, http: FakeHttp, tmp_path: Path
) -> None:
    http.default = None
    code, _, err = cli("update", "--_phase2", "--_backup", str(tmp_path / "bk"))
    assert code == 1 and f"wispralt restore {tmp_path / 'bk'}" in err


def test_uninstall_removes_only_artifacts(installed: Paths, cli: CliFn, runner: FakeRunner) -> None:
    sibling = installed.models_dir.parent / "keep-me.txt"
    sibling.write_text("mine")
    installed.models_dir.joinpath("hub", "x").mkdir(parents=True, exist_ok=True)
    installed.logs_dir.mkdir(parents=True, exist_ok=True)
    bk = installed.backup_dir / "wispralt-old"
    bk.mkdir(parents=True)
    code, _, _ = cli("uninstall")
    assert code == 0
    assert not installed.plist.exists() and not installed.models_dir.exists()
    assert not installed.logs_dir.exists()
    assert sibling.read_text() == "mine" and bk.is_dir()
    assert installed.db_path.exists() and installed.env_file.exists()
    assert runner.calls[-1] == ["uv", "tool", "uninstall", "wispralt"]
    state = InstallState.load(installed.state_file)
    assert state is not None and state.artifacts == ["uv-tool:wispralt"]


def test_tampered_state_deletes_nothing_outside_the_root(
    installed: Paths, cli: CliFn, tmp_path: Path
) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    state = InstallState.load(installed.state_file)
    assert state is not None
    state.artifacts.append(str(outside))
    state.save(installed.state_file)
    code, _, err = cli("uninstall")
    assert code == 0 and outside.is_dir() and "skipped" in err


def test_purge_data_backs_up_then_deletes(installed: Paths, cli: CliFn, runner: FakeRunner) -> None:
    code, out, _ = cli("uninstall", "--purge-data")
    assert code == 0 and "backup written" in out
    [bk] = _backups(installed)
    assert (bk / "wispralt.db").exists()
    assert not installed.data_dir.exists() and not installed.env_file.exists()
    assert not installed.state_file.exists()
    assert runner.calls[-1] == ["uv", "tool", "uninstall", "wispralt"]


def test_purge_no_backup(installed: Paths, cli: CliFn) -> None:
    assert cli("uninstall", "--purge-data", "--no-backup")[0] == 0
    assert not installed.data_dir.exists() and not installed.backup_dir.exists()


def test_purge_refuses_backup_dir_inside_data(
    installed: Paths, cli: CliFn, runner: FakeRunner
) -> None:
    state = InstallState.load(installed.state_file)
    assert state is not None
    state.paths["backup_dir"] = str(installed.data_dir / "backups")
    state.save(installed.state_file)
    code, _, err = cli("uninstall", "--purge-data")
    assert code == 2 and "inside" in err
    assert installed.db_path.exists() and installed.plist.exists() and runner.calls == []


def test_purge_without_state_is_refused(cli: CliFn) -> None:
    code, _, err = cli("uninstall", "--purge-data")
    assert code == 2 and "refusing" in err


def test_uninstall_client_moves_app_to_trash(
    cli: CliFn, runner: FakeRunner, tmp_path: Path
) -> None:
    app = make_app(Paths.resolve().app)
    transcripts = tmp_path / "home" / "Documents" / "WisprAlt Transcripts"
    transcripts.mkdir(parents=True)
    code, out, _ = cli("uninstall", "--client")
    assert code == 0 and "Trash" in out
    assert [
        "osascript",
        "-e",
        f'tell application "Finder" to delete POSIX file "{app}"',
    ] in runner.calls
    assert transcripts.is_dir()
    assert not any(c[0] == "security" for c in runner.calls)
