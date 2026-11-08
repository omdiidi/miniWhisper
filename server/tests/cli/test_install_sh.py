"""Task 9: install.sh and the kept scripts — syntax, the sourced-function smoke, no Python."""

from __future__ import annotations

import re
import shutil
import subprocess
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
