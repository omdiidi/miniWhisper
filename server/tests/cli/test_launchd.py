"""Task 2: the LaunchAgent plist, start/stop/restart, EIO retry, stop-proof, legacy agent."""

from __future__ import annotations

import plistlib
import sys
from typing import TYPE_CHECKING

import pytest

from wispralt.cli import launchd
from wispralt.cli.paths import Paths
from wispralt.cli.system import CliError
from wispralt.ops.backup import server_lock

from .conftest import LABEL, UID

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.paths import Ctx

    from .conftest import CliFn, FakeHttp, FakeRunner

# Captured from a throwaway agent on Darwin 27 (`/bin/sleep 600`), trimmed.
PRINT_RUNNING = f"""gui/{UID}/{LABEL} = {{
\tactive count = 1
\tpath = /tmp/{LABEL}.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /bin/sleep
\targuments = {{
\t\t/bin/sleep
\t\t600
\t}}

\tdomain = gui/{UID} [100023]
\truns = 1
\tpid = 29596
\timmediate reason = speculative
\tlast exit code = (never exited)
\tendpoints = {{
\t\t"x" = {{
\t\t\tstate = active
\t\t}}
\t}}
}}
"""
PRINT_CRASHED = f"""gui/{UID}/{LABEL} = {{
\tactive count = 0
\ttype = LaunchAgent
\tstate = not running

\truns = 3
\tlast exit code = 1
}}
"""
NOT_FOUND = f'Bad request.\nCould not find service "{LABEL}" in domain for user gui: {UID}\n'


@pytest.mark.parametrize(
    ("rc", "out", "expect"),
    [
        (0, PRINT_RUNNING, launchd.Service(True, "running", 29596, "(never exited)")),
        (0, PRINT_CRASHED, launchd.Service(True, "not running", None, "1")),
        (113, "", launchd.Service(False, "not loaded")),
    ],
)
def test_print_fixtures(
    ctx: Ctx, runner: FakeRunner, rc: int, out: str, expect: launchd.Service
) -> None:
    runner.on("launchctl", "print", rc=rc, out=out, err="" if rc == 0 else NOT_FOUND)
    assert launchd.service(ctx) == expect
    assert launchd.service(ctx).running is (expect.state == "running")


def test_plist_has_every_key_and_no_secret(ctx: Ctx) -> None:
    (ctx.paths.config_dir / ".env").write_text('WISPRALT_SECRET="deadbeef"\n')
    assert launchd.write_plist(ctx)
    raw = ctx.paths.plist.read_bytes()
    pl = plistlib.loads(raw)
    assert pl["Label"] == LABEL
    assert pl["ProgramArguments"] == [
        sys.executable, "-m", "uvicorn", "wispralt.main:app",
        "--host", "127.0.0.1", "--port", "8000", "--workers", "1",
    ]  # fmt: skip
    assert pl["WorkingDirectory"] == str(ctx.paths.config_dir)
    assert pl["EnvironmentVariables"] == {
        "PATH": launchd.LAUNCH_PATH,
        "HF_HOME": str(ctx.paths.models_dir),
    }
    assert pl["KeepAlive"] == {"SuccessfulExit": False}
    assert (pl["ThrottleInterval"], pl["ExitTimeOut"], pl["RunAtLoad"]) == (30, 15, True)
    assert pl["StandardOutPath"] == str(ctx.paths.logs_dir / "server.log")
    assert pl["StandardErrorPath"] == str(ctx.paths.logs_dir / "server.err.log")
    assert b"deadbeef" not in raw and b"API_KEY" not in raw
    assert not launchd.write_plist(ctx)  # same bytes → unchanged


def test_start_bootstraps_then_kickstarts(ctx: Ctx, runner: FakeRunner) -> None:
    launchd.start(ctx)
    assert ctx.paths.plist.exists()
    assert runner.verbs() == ["print", "bootstrap"]
    launchd.start(ctx)
    assert runner.verbs()[-2:] == ["print", "kickstart"]


def test_restart_same_bytes_is_kickstart_only(ctx: Ctx, runner: FakeRunner) -> None:
    launchd.start(ctx)
    runner.calls.clear()
    launchd.restart(ctx)
    assert runner.verbs() == ["print", "kickstart"]
    assert runner.calls[-1][2] == "-k"


def test_restart_changed_bytes_is_bootout_bootstrap(ctx: Ctx, runner: FakeRunner) -> None:
    launchd.start(ctx)
    assert ctx.state is not None
    ctx.state.port = 8123
    runner.calls.clear()
    launchd.restart(ctx)
    assert runner.verbs() == ["print", "print", "bootout", "bootstrap"]
    assert b"8123" in ctx.paths.plist.read_bytes()


def test_bootstrap_retries_eio(ctx: Ctx, runner: FakeRunner) -> None:
    runner.on(
        "launchctl", "bootstrap", rc=5, err="Bootstrap failed: 5: Input/output error", times=4
    )
    launchd.start(ctx)
    assert runner.verbs().count("bootstrap") == 5


def test_bootstrap_gives_up_after_five(ctx: Ctx, runner: FakeRunner) -> None:
    runner.on("launchctl", "bootstrap", rc=5, err="Bootstrap failed: 5: Input/output error")
    with pytest.raises(CliError) as exc:
        launchd.start(ctx)
    assert exc.value.code == 1 and runner.verbs().count("bootstrap") == 5


def test_other_bootstrap_errors_do_not_retry(ctx: Ctx, runner: FakeRunner) -> None:
    runner.on("launchctl", "bootstrap", rc=37, err="Operation already in progress")
    with pytest.raises(CliError):
        launchd.start(ctx)
    assert runner.verbs().count("bootstrap") == 1


def test_stop_waits_for_the_server_lock(ctx: Ctx, runner: FakeRunner) -> None:
    launchd.start(ctx)
    with server_lock(ctx.paths.data_dir), pytest.raises(CliError) as exc:
        launchd.stop(ctx)  # the "server" never lets go
    assert exc.value.code == 1 and "still holds" in exc.value.msg
    assert "bootout" in runner.verbs()
    assert launchd.stop(ctx) is False  # now unloaded and unlocked


def test_legacy_agent_is_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deps: object, runner: FakeRunner
) -> None:
    monkeypatch.setenv("WISPRALT_LABEL", "co.wispralt.host")
    from wispralt.cli.paths import Ctx

    c = Ctx(paths=Paths.resolve(), state=None, deps=deps)  # type: ignore[arg-type]
    c.paths.launch_agents.mkdir(parents=True)
    c.paths.legacy_plist.write_text("<plist/>")
    runner.loaded.add("co.wispralt.server")
    msg = launchd.retire_legacy(c)
    assert msg and "retired" in msg
    assert ["launchctl", "bootout", f"gui/{UID}/co.wispralt.server"] in runner.calls
    assert not c.paths.legacy_plist.exists()
    assert (c.paths.config_dir / "legacy" / "co.wispralt.server.plist").read_text() == "<plist/>"


def test_relabelled_install_never_touches_legacy(ctx: Ctx, runner: FakeRunner) -> None:
    ctx.paths.launch_agents.mkdir(parents=True)
    ctx.paths.legacy_plist.write_text("<plist/>")
    assert launchd.retire_legacy(ctx) is None
    assert runner.calls == [] and ctx.paths.legacy_plist.exists()


def test_commands(ctx: Ctx, cli: CliFn, runner: FakeRunner, http: FakeHttp) -> None:
    code, out, _ = cli("start")
    assert code == 0 and "healthy at http://127.0.0.1:8000" in out
    assert cli("stop")[1].strip() == "stopped"
    assert cli("stop")[1].strip() == "not running"
    http.default = None
    code, _, err = cli("restart")
    assert code == 1 and "server.err.log" in err


def test_commands_need_an_install(cli: CliFn) -> None:
    code, _, err = cli("start")
    assert code == 1 and "wispralt setup" in err
