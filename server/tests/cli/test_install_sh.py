"""Task 9: install.sh and the kept scripts — syntax, the sourced-function smoke, no Python."""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Captured before the cli conftest forbids them: these tests run bash on purpose.
_REAL_RUN, _REAL_POPEN = subprocess.run, subprocess.Popen
_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = sorted([_REPO / "install.sh", *(_REPO / "scripts").glob("*.sh")])


@pytest.fixture(autouse=True)
def _allow_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)


def _bash(*args: str) -> subprocess.CompletedProcess[str]:
    return _REAL_RUN(["bash", *args], cwd=_REPO, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name)
def test_syntax(script: Path) -> None:
    res = _bash("-n", str(script))
    assert res.returncode == 0, res.stderr


def test_install_sh_sourced_smoke() -> None:
    res = _bash(
        "-c",
        "set -euo pipefail; source ./install.sh; parse_flags; [ $NON_INTERACTIVE = 0 ];"
        " parse_flags --role host --non-interactive; [ $NON_INTERACTIVE = 1 ];"
        " require_clone; [ -f $CLONE/server/pyproject.toml ]; echo ok",
    )
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr


def test_install_sh_never_calls_python() -> None:
    text = (_REPO / "install.sh").read_text()
    assert "python3" not in text
    assert not re.search(r"^\s*sudo\b", text, re.MULTILINE)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_shellcheck() -> None:
    res = _REAL_RUN(["shellcheck", "install.sh"], cwd=_REPO, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_cloudflared_env_rewrite_is_never_world_readable(tmp_path: Path) -> None:
    """Review fix: persist_env_var's temp copy of .env (it holds the API key) is 0600 from
    the moment it exists, even under umask 000. A ``sed`` shim records the mode of the
    file it is writing into, then runs the real sed."""
    shim = tmp_path / "bin"
    shim.mkdir()
    log = tmp_path / "modes"
    real_sed = shutil.which("sed")
    (shim / "sed").write_text(
        "#!/bin/bash\n"
        f"{sys.executable} -c 'import os, stat; print(oct(stat.S_IMODE(os.fstat(1).st_mode))[2:],"
        f' file=open("{log}", "a"))\'\n'
        f'exec {real_sed} "$@"\n'
    )
    (shim / "sed").chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text('API_KEY="secret"\nSERVER_URL="http://127.0.0.1:8000"\n')
    env_file.chmod(0o600)
    script = _REPO / "scripts" / "setup-cloudflared.sh"
    res = _bash(
        "-c",
        "set -euo pipefail; umask 000;"
        f" eval \"$(sed -n '/^persist_env_var()/,/^}}/p' {script})\";"
        f" ENV_FILE={env_file}; PATH={shim}:$PATH;"
        " persist_env_var SERVER_URL https://t.example.com",
    )
    assert res.returncode == 0, res.stderr
    assert log.read_text().split() == ["600"]
    assert env_file.read_text() == 'API_KEY="secret"\nSERVER_URL=https://t.example.com\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".env.tmp")] == []


def test_cloudflared_legacy_plist_is_private_from_creation(tmp_path: Path) -> None:
    """Review fix: the legacy plist inlines the tunnel token, so it is 0600 the moment it
    exists, even under umask 000 and over an old 0644 plist. A ``chmod`` shim records the
    plist's mode just before the script's own ``chmod 0600``. Every external effect
    (sudo, launchctl, cloudflared, curl) is a shim; nothing reaches the real system."""
    home = tmp_path / "home"
    (home / ".config" / "wispralt").mkdir(parents=True)
    (home / ".config" / "wispralt" / ".env").write_text('SERVER_URL="http://127.0.0.1:8000"\n')
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "co.wispralt.cloudflared.plist"
    plist.write_text("old")
    plist.chmod(0o644)
    shim = tmp_path / "bin"
    shim.mkdir()
    log = tmp_path / "modes"
    real_chmod = shutil.which("chmod")
    shims = {
        "sudo": "exit 0",
        "launchctl": "exit 0",
        "brew": "exit 1",
        "curl": "printf 401",
        "cloudflared": 'if [ "$1" = version ]; then echo "cloudflared 2024.1.0"; fi',  # no --token-file
        "chmod": (
            f'case "${{@: -1}}" in *.plist) {sys.executable} -c'
            f" 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])'"
            f' "${{@: -1}}" >> {log};; esac\nexec {real_chmod} "$@"'
        ),
    }
    for name, body in shims.items():
        (shim / name).write_text(f"#!/bin/bash\n{body}\n")
        (shim / name).chmod(0o755)
    script = _REPO / "scripts" / "setup-cloudflared.sh"
    res = _REAL_RUN(
        ["bash", "-c", f"umask 000; PATH={shim}:$PATH HOME={home} WISPRALT_ROOT={home} bash {script}"],
        input="https://t.example.com\ntunnel-secret\n",
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    assert "tunnel-secret" in plist.read_text()
    assert log.read_text().split() == ["600"]
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600
