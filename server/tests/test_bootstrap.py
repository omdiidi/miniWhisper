"""
test_bootstrap.py — ``python -m wispralt.bootstrap`` as a subprocess.

Each run uses ``sys.executable`` in a tmp cwd with no ``.env`` and an explicit
env (no ``WISPRALT_*``, ``DATA_DIR`` pointed at a tmp dir), so the real
``server/.env`` and venv are never touched. Token-printing modes run under a
pty, because they refuse a non-terminal stdout.
"""

from __future__ import annotations

import os
import pty
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wispralt.accounts.service import hash_token
from wispralt.accounts.store import AccountStore
from wispralt.database import Database

_TOKEN = re.compile(r"\b[0-9a-f]{64}\b")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    base = {
        k: v for k, v in os.environ.items() if not k.startswith("WISPRALT_") and k != "DATA_DIR"
    }
    return {**base, "DATA_DIR": str(tmp_path / "data"), "SERVER_URL": "https://t.example"}


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    d = tmp_path / "cwd"
    d.mkdir()
    return d


def _cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "wispralt.bootstrap", *args]


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_cmd(*args), env=env, cwd=cwd, capture_output=True, text=True, timeout=60)


def _run_tty(args: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    master, slave = pty.openpty()
    proc = subprocess.Popen(_cmd(*args), env=env, cwd=cwd, stdout=slave, stderr=subprocess.PIPE)
    os.close(slave)
    chunks: list[bytes] = []
    while True:
        try:
            data = os.read(master, 4096)
        except OSError:  # EIO once the child closed the pty
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    _out, err = proc.communicate(timeout=60)
    return proc.returncode, b"".join(chunks).decode(), err.decode()


def _accounts(env: dict[str, str]) -> AccountStore:
    return AccountStore(Database(Path(env["DATA_DIR"]) / "wispralt.db"))


def test_non_tty_refuses_before_creating_anything(env: dict[str, str], cwd: Path) -> None:
    res = _run(["--label", "owner"], env, cwd)
    assert res.returncode == 2
    assert not _TOKEN.search(res.stdout + res.stderr)
    assert not Path(env["DATA_DIR"]).exists()
    res = _run(["--rotate-admin"], env, cwd)
    assert res.returncode == 2


def test_bootstrap_rotate_and_check(env: dict[str, str], cwd: Path) -> None:
    assert _run(["--check"], env, cwd).returncode == 1  # no DB yet, nothing created
    assert not Path(env["DATA_DIR"]).exists()

    code, out, _err = _run_tty(["--label", "owner"], env, cwd)
    assert code == 0, out
    assert "https://t.example" in out
    tokens = _TOKEN.findall(out)
    assert len(tokens) == 1
    first = tokens[0]
    data_dir = Path(env["DATA_DIR"])
    assert oct(data_dir.stat().st_mode & 0o777) == oct(0o700)
    db = Database(data_dir / "wispralt.db")
    with db.read() as con:
        assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1
    admin = _accounts(env).lookup_key(hash_token(first))
    assert admin is not None and admin.role == "admin" and admin.label == "owner"

    code, out, err = _run_tty(["--label", "again"], env, cwd)
    assert code == 1 and "admin already exists" in err and not _TOKEN.search(out)

    assert _run(["--check"], env, cwd).returncode == 0

    code, out, _err = _run_tty(["--rotate-admin"], env, cwd)
    assert code == 0
    [second] = _TOKEN.findall(out)
    assert _accounts(env).lookup_key(hash_token(first)) is None
    rotated = _accounts(env).lookup_key(hash_token(second))
    assert rotated is not None and rotated.id == admin.id and rotated.role == "admin"
    assert list(cwd.iterdir()) == []


def test_rotate_admin_without_admin_fails(env: dict[str, str], cwd: Path) -> None:
    code, out, err = _run_tty(["--rotate-admin"], env, cwd)
    assert code == 1 and "run without --rotate-admin" in err and not _TOKEN.search(out)


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_bad_data_dir_creates_nothing(env: dict[str, str], cwd: Path, value: str) -> None:
    code, out, err = _run_tty(["--label", "owner"], {**env, "DATA_DIR": value}, cwd)
    assert code != 0 and "DATA_DIR" in err and not _TOKEN.search(out)
    assert list(cwd.iterdir()) == []


def test_one_copy_of_the_key_handover_text() -> None:
    """Review fix: bootstrap.py and the wispralt CLI print the key through one function."""
    from wispralt.cli import host

    src = Path(host.__file__).resolve().parents[1]
    owners = [f for f in src.rglob("*.py") if "shown ONCE" in f.read_text()]
    assert owners == [Path(host.__file__).resolve()]
    assert "host.client_config_text(" in (src / "bootstrap.py").read_text()
    text = host.client_config_text("", "k" * 64, heading="First admin created.")
    assert "not set yet" in text and "k" * 64 in text
