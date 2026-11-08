"""Task 1: paths, install-state, .env merge, the entry point and its import hygiene."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from wispralt.cli import paths as p
from wispralt.cli.main import parse
from wispralt.cli.system import CliError

if TYPE_CHECKING:
    from .conftest import CliFn

_REAL_RUN, _REAL_POPEN = (
    subprocess.run,
    subprocess.Popen,
)  # captured before the fixture forbids them
_SRC = Path(__file__).resolve().parents[2] / "src"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_root_reroots_every_path(tmp_path: Path) -> None:
    paths = p.Paths.resolve()
    root = tmp_path / "root"
    for loc in (
        paths.config_dir,
        paths.env_file,
        paths.state_file,
        paths.data_dir,
        paths.models_dir,
        paths.logs_dir,
        paths.backup_dir,
        paths.db_path,
    ):
        assert loc.is_relative_to(root), loc
    assert paths.plist == tmp_path / "home" / "Library" / "LaunchAgents" / "co.wispralt.test.plist"
    assert paths.label == "co.wispralt.test"


def test_flag_dirs_override(tmp_path: Path) -> None:
    paths = p.Paths.resolve(data_dir=tmp_path / "d", backup_dir=tmp_path / "b")
    assert (paths.data_dir, paths.backup_dir) == (tmp_path / "d", tmp_path / "b")


def test_state_round_trip_is_private(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "install-state.json"
    assert p.InstallState.load(path) is None
    st = p.InstallState(version="1", role="host", port=8123)
    st.add("artifacts", Path("/x"), Path("/x"))
    st.save(path)
    again = p.InstallState.load(path)
    assert again is not None and again.role == "host" and again.port == 8123
    assert again.artifacts == ["/x"] and again.last_run is not None
    assert _mode(path) == 0o600 and _mode(path.parent) == 0o700


def test_atomic_write_keeps_old_file_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cfg" / "install-state.json"
    p.InstallState(role="host").save(path)
    before = path.read_text()

    def _crash(*_a: object, **_k: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", _crash)
    with pytest.raises(OSError):
        p.InstallState(role="both").save(path)
    assert path.read_text() == before
    assert [f.name for f in path.parent.iterdir()] == ["install-state.json"]  # no tmp left


def test_unreadable_state_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{nope")
    with pytest.raises(CliError) as exc:
        p.InstallState.load(path)
    assert exc.value.code == 1


def test_merge_missing_never_overwrites_and_keeps_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# my note\nSERVER_URL=https://mine.example\nexport FOO=1")
    added = p.merge_missing(env, {"SERVER_URL": "http://127.0.0.1:8000", "DATA_DIR": "/a b/c"})
    assert added == ["DATA_DIR"]
    assert env.read_text() == (
        '# my note\nSERVER_URL=https://mine.example\nexport FOO=1\nDATA_DIR="/a b/c"\n'
    )
    assert _mode(env) == 0o600
    mtime = env.stat().st_mtime_ns
    assert p.merge_missing(env, {"DATA_DIR": "/other", "FOO": "2"}) == []
    assert env.stat().st_mtime_ns == mtime
    from dotenv import dotenv_values

    assert dotenv_values(env)["DATA_DIR"] == "/a b/c"


def test_merge_missing_refuses_quotes(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        p.merge_missing(tmp_path / ".env", {"X": 'a"b'})


def test_help_lists_every_command(cli: CliFn) -> None:
    code, out, _ = cli("--help")
    assert code == 0
    for name in (
        "setup", "status", "doctor", "start", "stop", "restart", "users", "keys",
        "retention", "backup", "restore", "update", "uninstall",
    ):  # fmt: skip
        assert name in out


def test_global_flags_work_after_the_command() -> None:
    args = parse(["status", "--json", "--non-interactive"])
    assert args.json and args.non_interactive and not args.yes
    args = parse(["--yes", "keys", "revoke", "3"])
    assert args.yes and args.key_id == 3


def test_import_without_mlx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in {'mlx', 'parakeet_mlx', 'mlx_whisper'}:\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import importlib, pkgutil, wispralt.cli\n"
        "for m in pkgutil.iter_modules(wispralt.cli.__path__):\n"
        "    importlib.import_module('wispralt.cli.' + m.name)\n"
        "assert not {'mlx', 'parakeet_mlx', 'mlx_whisper', 'wispralt.config'} & set(sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"DATA_DIR", "SERVER_URL"}}
    env["PYTHONPATH"] = str(_SRC)
    res = _REAL_RUN(
        [sys.executable, "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


def test_unknown_state_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"role": "client", "future": 1}))
    st = p.InstallState.load(path)
    assert st is not None and st.role == "client"


def test_set_values_replaces_in_place_and_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# note\nexport SERVER_URL=old\nA=1\nSERVER_URL=dup")
    assert p.set_values(env, {"SERVER_URL": "https://new", "B": "2"}) == ["SERVER_URL", "B"]
    assert env.read_text() == '# note\nSERVER_URL="https://new"\nA=1\nB="2"\n'
    assert p.set_values(env, {"SERVER_URL": "https://new"}) == []
    with pytest.raises(p.CliError):
        p.set_values(env, {"X": 'a"b'})
