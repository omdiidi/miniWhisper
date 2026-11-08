"""
cli/launchd.py — the host service as a per-user LaunchAgent (``gui/$UID``).

The plist is rendered from a dict (``plistlib``), never templated text, and
holds no secret: the server reads the config ``.env`` from its
``WorkingDirectory``. ``ProgramArguments`` starts uvicorn with
``sys.executable`` — the ``uv tool`` venv's interpreter, a path that survives
``uv tool install --force``. The service binds loopback only.

"Stopped" is proven only by acquiring ``ops.backup.server_lock`` (the lock the
server holds for its whole life); ``launchctl bootout`` returns before teardown
finishes, and an immediate ``bootstrap`` can fail with EIO, so it is retried.
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wispralt._errors import ServerRunning
from wispralt.cli.paths import DEFAULT_LABEL, LEGACY_LABEL, private_dir, write_private
from wispralt.cli.system import CliError, failure
from wispralt.ops.backup import server_lock

if TYPE_CHECKING:
    import argparse

    from wispralt.cli.paths import Ctx

LAUNCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STOP_TIMEOUT_S = 20.0
HEALTH_TIMEOUT_S = 120.0  # the lifespan loads the dictation model before /healthz answers
_POLL_S = 0.25
_EIO_RETRIES = 5
_FIELD = re.compile(r"^\t(state|pid|last exit code) = (.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Service:
    loaded: bool
    state: str  # "running", "not running", … or "not loaded"
    pid: int | None = None
    last_exit: str | None = None

    @property
    def running(self) -> bool:
        return self.state == "running" and self.pid is not None


def domain() -> str:
    return f"gui/{os.getuid()}"


def service(ctx: Ctx, label: str | None = None) -> Service:
    """Parse ``launchctl print``; exit 113 (or any failure) means not loaded."""
    res = ctx.runner.run(
        ["launchctl", "print", f"{domain()}/{label or ctx.paths.label}"], check=False
    )
    if res.returncode != 0:
        return Service(loaded=False, state="not loaded")
    fields = dict(_FIELD.findall(res.stdout))
    pid = fields.get("pid")
    return Service(
        loaded=True,
        state=fields.get("state", "unknown"),
        pid=int(pid) if pid and pid.isdigit() else None,
        last_exit=fields.get("last exit code"),
    )


def render(ctx: Ctx) -> bytes:
    p = ctx.paths
    plist = {
        "Label": p.label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "uvicorn",
            "wispralt.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(ctx.port),
            "--workers",
            "1",
        ],  # fmt: skip
        "WorkingDirectory": str(p.config_dir),  # Settings reads .env from the cwd
        "EnvironmentVariables": {"PATH": LAUNCH_PATH, "HF_HOME": str(p.models_dir)},
        "KeepAlive": {"SuccessfulExit": False},  # restart only on an unexpected exit
        "ThrottleInterval": 30,
        "ExitTimeOut": 15,  # SIGTERM grace before SIGKILL
        "StandardOutPath": str(p.logs_dir / "server.log"),
        "StandardErrorPath": str(p.logs_dir / "server.err.log"),
        "RunAtLoad": True,
    }
    return plistlib.dumps(plist)


def write_plist(ctx: Ctx) -> bool:
    """Write the plist if its bytes changed; True when they did."""
    data = render(ctx)
    path = ctx.paths.plist
    if path.exists() and path.read_bytes() == data:
        return False
    private_dir(ctx.paths.logs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, data.decode())
    path.chmod(0o644)  # launchd reads it; it holds no secret
    return True


def _bootstrap(ctx: Ctx) -> None:
    argv = ["launchctl", "bootstrap", domain(), str(ctx.paths.plist)]
    for attempt in range(1, _EIO_RETRIES + 1):
        res = ctx.runner.run(argv, check=False)
        if res.returncode == 0:
            return
        eio = res.returncode == 5 or "Input/output error" in (res.stderr or "")
        if not eio or attempt == _EIO_RETRIES:
            raise CliError(1, failure(argv, res))
        ctx.deps.sleep(1.0)


def _bootout(ctx: Ctx, label: str | None = None) -> None:
    """Boot the agent out and wait until launchd stops listing it.

    ``launchctl bootout`` returns before the job is gone; a ``start`` in that window
    sees it still loaded, kickstarts a dying job and leaves the service unloaded.
    """
    label = label or ctx.paths.label
    ctx.runner.run(["launchctl", "bootout", f"{domain()}/{label}"], check=False)
    for _ in range(max(1, int(STOP_TIMEOUT_S / _POLL_S))):
        if not service(ctx, label).loaded:
            return
        ctx.deps.sleep(_POLL_S)
    raise CliError(1, f"launchd still lists {label} {STOP_TIMEOUT_S:.0f} s after bootout")


def wait_stopped(ctx: Ctx, timeout: float = STOP_TIMEOUT_S) -> None:
    """Return once ``server_lock(data_dir)`` can be taken, else ``CliError(1)``."""
    data_dir = ctx.paths.data_dir
    if not data_dir.parent.is_dir():
        return  # no data dir parent → no lock file → no server on this data
    for _ in range(max(1, int(timeout / _POLL_S))):
        try:
            with server_lock(data_dir):
                return
        except ServerRunning:
            ctx.deps.sleep(_POLL_S)
    raise CliError(1, f"the service still holds {data_dir}.lock after {timeout:.0f} s")


def wait_healthy(ctx: Ctx, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    for _ in range(max(1, int(timeout))):
        if ctx.deps.http_get(f"{ctx.url}/healthz", 2.0) == 200:
            return True
        ctx.deps.sleep(1.0)
    return False


def start(ctx: Ctx) -> None:
    """Bootstrap when not loaded (writing the plist first if missing), else kickstart."""
    if not service(ctx).loaded:
        if not ctx.paths.plist.exists():
            write_plist(ctx)
        _bootstrap(ctx)
    else:
        ctx.runner.run(["launchctl", "kickstart", f"{domain()}/{ctx.paths.label}"])


def stop(ctx: Ctx) -> bool:
    """Boot the agent out and prove the server let go of its lock; True if it was loaded."""
    loaded = service(ctx).loaded
    if loaded:
        _bootout(ctx)
    wait_stopped(ctx)
    return loaded


def restart(ctx: Ctx) -> None:
    """Re-render the plist: changed → bootout + bootstrap; same → ``kickstart -k``."""
    changed = write_plist(ctx)
    if not service(ctx).loaded:
        _bootstrap(ctx)
    elif changed:
        stop(ctx)
        _bootstrap(ctx)
    else:
        ctx.runner.run(["launchctl", "kickstart", "-k", f"{domain()}/{ctx.paths.label}"])


def remove(ctx: Ctx) -> None:
    """Boot out (if loaded) and delete the plist."""
    if service(ctx).loaded:
        _bootout(ctx)
    ctx.paths.plist.unlink(missing_ok=True)


def retire_legacy(ctx: Ctx) -> str | None:
    """Boot out the mini-era ``co.wispralt.server`` and move its plist to ``config/legacy/``.

    Only for the default label: a re-labelled (test / E2E) install must never touch
    the real host's agents.
    """
    if ctx.paths.label != DEFAULT_LABEL:
        return None
    loaded = service(ctx, LEGACY_LABEL).loaded
    if loaded:
        _bootout(ctx, LEGACY_LABEL)
    src = ctx.paths.legacy_plist
    if not src.exists():
        return f"booted out legacy {LEGACY_LABEL}" if loaded else None
    dest_dir = ctx.paths.config_dir / "legacy"
    private_dir(dest_dir)
    os.replace(src, dest_dir / src.name)
    return f"retired legacy {LEGACY_LABEL}; its plist is in {dest_dir}"


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_start(ctx: Ctx, _args: argparse.Namespace) -> int:
    ctx.need_state()
    start(ctx)
    return _report_health(ctx, "started")


def cmd_stop(ctx: Ctx, _args: argparse.Namespace) -> int:
    ctx.need_state()
    print("stopped" if stop(ctx) else "not running")
    return 0


def cmd_restart(ctx: Ctx, _args: argparse.Namespace) -> int:
    ctx.need_state()
    restart(ctx)
    return _report_health(ctx, "restarted")


def _report_health(ctx: Ctx, verb: str) -> int:
    if wait_healthy(ctx):
        print(f"{verb}; healthy at {ctx.url}")
        return 0
    print(
        f"{verb}, but {ctx.url}/healthz did not answer; see {ctx.paths.logs_dir}/server.err.log",
        file=sys.stderr,
    )
    return 1
