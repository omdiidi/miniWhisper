"""Task 8: update (two phases across an execv) and uninstall (only what state lists)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from wispralt.cli import ops
from wispralt.cli.host import installed_exe
from wispralt.cli.paths import InstallState, Paths
from wispralt.ops.backup import server_lock

from .conftest import make_app

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.system import Deps

    from .conftest import CliFn, Execs, FakeHttp, FakeHub, FakeRunner

EXE = installed_exe()
JOB = "0b6e3f0c-6c1e-4d3a-9f57-2a8d1c4e5b6f"  # a jobs/store.py id (uuid4)
UPLOAD = "AbCdEfGhIjKlMnOpQr_-12"  # a token_urlsafe(16) upload id


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
    assert steps == ["git -C", "git -C", "launchctl bootout", "uv tool", f"{EXE} --version"]
    assert runner.calls[0][3:] == ["status", "--porcelain"]
    assert runner.calls[1][3:] == ["pull", "--ff-only"]
    assert runner.calls[-2] == [
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


def test_failed_backup_restarts_the_old_service(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any phase-1 error (here: disk full during VACUUM INTO), not only a CliError."""

    def disk_full(*_a: object, **_k: object) -> Path:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(ops.offline, "backup", disk_full)
    code, _, err = cli("update")
    assert code == 1 and "database or disk is full" in err and "restarted" in err
    assert runner.verbs()[-1] == "bootstrap" and execs.calls == []
    assert not any(c[:2] == ["uv", "tool"] for c in runner.calls)


def test_interrupted_phase1_still_restarts_the_old_service(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_a: object, **_k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "reinstall_cli", interrupted)
    with pytest.raises(RuntimeError, match="boom"):
        cli("update", "--no-backup")
    assert runner.verbs()[-1] == "bootstrap" and execs.calls == []


def test_stop_timeout_rebootstraps_the_agent(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs
) -> None:
    """R3 fix: the bootout happened, then the lock wait timed out: the agent comes back."""
    with server_lock(installed.data_dir):  # the "old server" never lets go
        code, _, err = cli("update", "--no-backup")
    assert code == 1 and "still holds" in err and "restarted" in err
    verbs = runner.verbs()
    assert "bootout" in verbs and verbs[-1] == "bootstrap" and execs.calls == []
    assert not any(c[:2] == ["uv", "tool"] for c in runner.calls)


def test_missing_new_executable_restarts_the_service(
    installed: Paths, cli: CliFn, runner: FakeRunner, deps: Deps
) -> None:
    """R3 fix: an execv that fails (binary gone) must not leave the host stopped."""

    def missing(_argv: list[str]) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    deps.execv = missing
    code, _, err = cli("update")
    assert code == 1 and "cannot run" in err and "FileNotFoundError" in err
    assert "restarted and is answering" in err and "wispralt restore" in err
    assert runner.verbs()[-1] == "bootstrap"


def test_broken_new_executable_restarts_without_exec(
    installed: Paths, cli: CliFn, runner: FakeRunner, execs: Execs
) -> None:
    """R3 fix: a new install that cannot even print its version is never exec'd into."""
    runner.on(EXE, rc=1, err="ModuleNotFoundError: No module named 'wispralt'")
    code, _, err = cli("update", "--no-backup")
    assert code == 1 and "ModuleNotFoundError" in err and "restarted" in err
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


def test_uninstall_resolves_dotdot_and_symlinked_parents(
    installed: Paths, cli: CliFn, tmp_path: Path
) -> None:
    """R3 fix: containment is checked on resolved paths, not lexically."""
    outside = tmp_path / "precious"
    (outside / "victim").mkdir(parents=True)
    escape = installed.root / "escape"
    escape.symlink_to(outside)  # a parent inside the root that points out of it
    link = installed.root / "link-out"
    link.symlink_to(outside / "victim")  # an artifact that is itself a symlink
    state = InstallState.load(installed.state_file)
    assert state is not None
    state.artifacts += [
        str(escape / "victim"),
        str(installed.root / ".." / "precious"),
        str(installed.root / "." / "escape" / ".." / "precious" / "victim"),
        str(link),
    ]
    state.save(installed.state_file)
    code, _, err = cli("uninstall")
    assert code == 0 and err.count("skipped") == 3
    assert (outside / "victim").is_dir()
    assert not link.is_symlink() and not link.exists()  # unlinked, its target untouched


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


def test_failed_phase2_still_restarts_the_service(
    installed: Paths, cli: CliFn, runner: FakeRunner, hub: FakeHub, tmp_path: Path
) -> None:
    """Review fix: model verification failing after the reinstall must not leave it stopped."""
    runner.on("launchctl", "print", rc=113)  # phase 1 booted it out: not loaded
    hub.offline = True
    state = InstallState.load(installed.state_file)
    assert state is not None
    state.models = {}  # no verified copy to fall back on
    state.save(installed.state_file)
    code, _, err = cli("update", "--_phase2", "--_backup", str(tmp_path / "bk"))
    assert code == 1 and "update failed after the reinstall" in err
    assert "bootstrap" in runner.verbs()  # it tried to start the service again
    assert "restarted and is answering" in err and f"wispralt restore {tmp_path / 'bk'}" in err


def test_failed_phase2_reports_a_failed_restart(
    installed: Paths, cli: CliFn, runner: FakeRunner, hub: FakeHub
) -> None:
    runner.on("launchctl", "print", rc=113)
    runner.on("launchctl", "bootstrap", rc=1, err="Bootstrap failed: 5")
    hub.offline = True
    state = InstallState.load(installed.state_file)
    assert state is not None
    state.models = {}
    state.save(installed.state_file)
    code, _, err = cli("update", "--_phase2")
    assert code == 1 and "could NOT be restarted" in err and "wispralt start" in err


def test_purge_deletes_only_what_wispralt_owns(
    installed: Paths, cli: CliFn, tmp_path: Path
) -> None:
    """Review fix: a data dir the owner also uses keeps its other files, and the dir."""
    mine = installed.data_dir / "my-notes.txt"
    mine.write_text("not WisprAlt's")
    (installed.data_dir / "outputs").mkdir(exist_ok=True)
    (installed.data_dir / "outputs" / f"{JOB}.txt").write_text("t")
    code, out, _ = cli("uninstall", "--purge-data", "--no-backup")
    assert code == 0 and "other files there were kept" in out
    assert mine.read_text() == "not WisprAlt's"
    assert sorted(x.name for x in installed.data_dir.iterdir()) == ["my-notes.txt"]
    assert not installed.env_file.exists() and not installed.state_file.exists()


def test_purge_without_a_db_still_backs_up_the_files(installed: Paths, cli: CliFn) -> None:
    for suffix in ("", "-wal", "-shm"):
        installed.db_path.with_name(installed.db_path.name + suffix).unlink(missing_ok=True)
    (installed.data_dir / "outputs").mkdir(exist_ok=True)
    (installed.data_dir / "outputs" / f"{JOB}.txt").write_text("transcript")
    code, out, _ = cli("uninstall", "--purge-data")
    assert code == 0 and "backup written" in out and "no database" in out
    [copy] = [d for d in installed.backup_dir.iterdir() if d.name.endswith("-files")]
    assert (copy / "outputs" / f"{JOB}.txt").read_text() == "transcript"
    assert not installed.data_dir.exists()


def test_purge_keeps_foreign_files_inside_outputs_and_staging(installed: Paths, cli: CliFn) -> None:
    """R4 fix: a shared data dir that holds the DB keeps what WisprAlt did not write."""
    data = installed.data_dir
    out, stage = data / "outputs", data / "staging"
    chunk = stage / "chunked" / UPLOAD
    for d in (out, chunk):
        d.mkdir(parents=True, exist_ok=True)
    ours = [
        out / f"{JOB}.txt", out / f"{JOB}.json", out / f".import-{JOB}.srt.part",
        out / "tmpab_c12x9.tmp", stage / f"{JOB}.wav", stage / f"{'a' * 32}.m4a",
        stage / f"{UPLOAD}.mp3", stage / f"{UPLOAD}.wav.partial",
        stage / f"{JOB}_canonical.wav", chunk / "meta.json", chunk / "chunk-0000.part",
        chunk / "chunk-0001.part.tmp",
    ]  # fmt: skip
    foreign = [out / "notes.txt", out / "photos" / "a.jpg", stage / "budget.xlsx"]
    foreign += [chunk / "readme.md"]
    for f in ours + foreign:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    code, text, _ = cli("uninstall", "--purge-data", "--no-backup")
    assert code == 0 and "other files there were kept" in text
    assert not any(f.exists() for f in ours)
    assert all(f.read_text() == "x" for f in foreign)
    for kept in (out / "notes.txt", out / "photos", stage / "budget.xlsx", chunk / "readme.md"):
        assert str(kept) in text
    assert not installed.db_path.exists()


def test_purge_patterns_cover_what_the_server_writes() -> None:
    """The purge name lists are copies; they must track the modules that write the files."""
    from wispralt.files.output import _OUTPUT_SUFFIXES
    from wispralt.importer.files import PRIMARY_ORDER
    from wispralt.ops.staging import _ALLOWED_EXTENSIONS

    assert set(ops._TRANSCRIPT_EXTS) == set(_OUTPUT_SUFFIXES) == set(PRIMARY_ORDER)
    assert set(ops._AUDIO_EXTS) == set(_ALLOWED_EXTENSIONS)
