"""Task 6: setup host — a fresh run, an idempotent rerun, the [host] extra, model checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values

from wispralt.accounts.service import hash_token
from wispralt.accounts.store import AccountStore
from wispralt.cli import host
from wispralt.cli.paths import InstallState, Paths
from wispralt.database import Database
from wispralt.dictate.parakeet import MODEL_ID as DICTATION_MODEL
from wispralt.jobs.store import JobStore

if TYPE_CHECKING:
    from pathlib import Path

    from wispralt.cli.system import Deps

    from .conftest import CliFn, Execs, FakeHttp, FakeRunner

_TOKEN = re.compile(r"\b[0-9a-f]{64}\b")
COMMIT = "c0ffee" + "0" * 34


@dataclass
class FakeHub:
    """Two tiny "models" (one LFS file with a sha256, one small file checked by size)."""

    root: Path
    corrupt: int = 0  # the next N downloads write bad bytes
    offline: bool = False
    downloads: list[tuple[str, bool]] = field(default_factory=list)

    def _files(self, repo: str) -> dict[str, bytes]:
        return {"weights.safetensors": repo.encode() * 100, "config.json": b"{}"}

    def resolve(self, repo: str) -> tuple[str, list[host.RemoteFile]]:
        if self.offline:
            raise host.HubError("ConnectError: offline")
        return COMMIT, [
            host.RemoteFile(
                n, len(b), hashlib.sha256(b).hexdigest() if n.endswith("tensors") else None
            )
            for n, b in self._files(repo).items()
        ]

    def snapshot(
        self, repo: str, commit: str, *, download: bool, force: bool = False
    ) -> Path | None:
        path = self.root / repo / commit
        if not download:
            return path if path.is_dir() else None
        self.downloads.append((repo, force))
        path.mkdir(parents=True, exist_ok=True)
        for name, data in self._files(repo).items():
            bad = self.corrupt > 0 and name.endswith("tensors")
            (path / name).write_bytes(data[::-1] if bad else data)
        self.corrupt = max(0, self.corrupt - 1)
        return path


@pytest.fixture
def hub(tmp_path: Path, deps: Deps) -> FakeHub:
    fake = FakeHub(tmp_path / "hub")
    deps.hub = fake
    deps.load_fn = lambda _id: (lambda _samples: SimpleNamespace(text=""))
    return fake


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    src = tmp_path / "clone"
    (src / "server").mkdir(parents=True)
    (src / "server" / "pyproject.toml").write_text("[project]\nname='wispralt'\n")
    return src


def _setup(cli: CliFn, clone: Path, *extra: str) -> tuple[int, str, str]:
    return cli("setup", "--role", "host", "--non-interactive", "--source", str(clone), *extra)


def test_fresh_run(
    cli: CliFn, hub: FakeHub, clone: Path, runner: FakeRunner, tmp_path: Path
) -> None:
    tok = tmp_path / "admin.key"
    code, out, err = _setup(cli, clone, "--token-file", str(tok))
    assert code == 0, err
    paths = Paths.resolve()
    state = InstallState.load(paths.state_file)
    assert state is not None and state.role == "host" and state.source_dir == str(clone)
    assert state.retention_prompted and len(state.models) == 2
    assert set(state.models.values()) == {COMMIT}
    assert host.UV_TOOL in state.artifacts and str(paths.plist) in state.artifacts
    assert str(paths.data_dir) in state.data and str(paths.env_file) in state.data
    env = dotenv_values(paths.env_file)
    assert env["DATA_DIR"] == str(paths.data_dir) and env["DATA_DIR"].startswith("/")
    assert env["SERVER_URL"] == "http://127.0.0.1:8000"
    accounts = AccountStore(Database(paths.db_path))
    [admin] = accounts.list_enabled_admins()
    token = tok.read_text().strip()
    assert accounts.lookup_key(hash_token(token)).id == admin.id  # type: ignore[union-attr]
    assert admin.label == "owner"
    assert paths.plist.exists() and "bootstrap" in runner.verbs()
    assert ["open", "http://127.0.0.1:8000/admin"] in runner.calls
    assert len(hub.downloads) == 2
    assert token not in runner.argv_text() + out + err
    assert not any(token in (i or "") for i in runner.inputs)


def test_rerun_changes_nothing(
    cli: CliFn, hub: FakeHub, clone: Path, runner: FakeRunner, tmp_path: Path
) -> None:
    assert _setup(cli, clone, "--token-file", str(tmp_path / "a.key"))[0] == 0
    paths = Paths.resolve()
    env_before = paths.env_file.read_bytes()
    state_before = json.loads(paths.state_file.read_text())
    runner.calls.clear()
    hub.downloads.clear()

    code, out, err = _setup(cli, clone, "--no-open")  # no --token-file needed: nothing minted
    assert code == 0, err
    assert "admin exists" in out and not _TOKEN.search(out + err)
    assert hub.downloads == []
    assert paths.env_file.read_bytes() == env_before
    assert runner.verbs() == ["print", "kickstart"]  # retire_legacy skipped (test label)
    state_after = json.loads(paths.state_file.read_text())
    assert {k: v for k, v in state_after.items() if k != "last_run"} == {
        k: v for k, v in state_before.items() if k != "last_run"
    }
    assert len(AccountStore(Database(paths.db_path)).list_enabled_admins()) == 1


def test_missing_extra_reinstalls_then_execs(
    cli: CliFn,
    hub: FakeHub,
    clone: Path,
    runner: FakeRunner,
    execs: Execs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "has_host_extra", lambda: False)
    code, _, _ = _setup(cli, clone, "--no-open")
    assert code == 0
    assert runner.calls == [
        ["uv", "tool", "install", "--force", "--python", "3.11", f"{clone}/server[host]"]
    ]
    [argv] = execs.calls
    assert argv[0].endswith("/wispralt")
    assert argv[1:] == ["setup", "--role", "host", "--non-interactive", "--source", str(clone),
                        "--no-open", "--_resumed"]  # fmt: skip
    assert hub.downloads == []
    code, _, err = _setup(cli, clone, "--no-open", "--_resumed")  # still missing → stop
    assert code == 1 and "MLX stack is missing" in err


def test_sha_mismatch_redownloads_once_then_fails(
    cli: CliFn, hub: FakeHub, clone: Path, tmp_path: Path
) -> None:
    hub.corrupt = 2
    code, _, err = _setup(cli, clone, "--token-file", str(tmp_path / "a.key"))
    assert code == 1 and "failed verification" in err
    assert hub.downloads == [(DICTATION_MODEL, False), (DICTATION_MODEL, True)]


def test_one_bad_download_is_repaired(
    cli: CliFn, hub: FakeHub, clone: Path, tmp_path: Path
) -> None:
    hub.corrupt = 1
    assert _setup(cli, clone, "--token-file", str(tmp_path / "a.key"), "--no-open")[0] == 0
    assert [force for _, force in hub.downloads] == [False, True, False]


def test_offline_rerun_keeps_the_verified_copy(
    cli: CliFn, hub: FakeHub, clone: Path, tmp_path: Path
) -> None:
    assert _setup(cli, clone, "--token-file", str(tmp_path / "a.key"), "--no-open")[0] == 0
    hub.offline = True
    code, out, _ = _setup(cli, clone, "--no-open")
    assert code == 0 and "keeping the verified copy" in out


def test_non_arm64_explains(cli: CliFn, hub: FakeHub, clone: Path, deps: Deps) -> None:
    deps.machine = "x86_64"
    code, _, err = _setup(cli, clone)
    assert code == 1 and "Apple Silicon" in err and hub.downloads == []


def test_missing_role_non_interactive(cli: CliFn) -> None:
    code, _, err = cli("setup", "--non-interactive")
    assert code == 2 and "--role" in err


def test_no_tty_no_token_file_refuses_before_downloading(
    cli: CliFn, hub: FakeHub, clone: Path
) -> None:
    code, _, err = _setup(cli, clone)
    assert code == 2 and "--token-file" in err
    assert hub.downloads == [] and not Paths.resolve().db_path.exists()


def test_interactive_asks_role_label_retention(
    cli: CliFn, hub: FakeHub, clone: Path, deps: Deps, answers: list[str], runner: FakeRunner
) -> None:
    deps.stdin_isatty = deps.stdout_isatty = True
    answers.extend(["host", "alice", "30"])
    code, out, err = cli("setup", "--source", str(clone), "--no-open")
    assert code == 0, err
    [token] = _TOKEN.findall(out)
    paths = Paths.resolve()
    db = Database(paths.db_path)
    [admin] = AccountStore(db).list_enabled_admins()
    assert admin.label == "alice" and JobStore(db, paths.data_dir).retention_days() == 30
    assert token not in runner.argv_text()


def test_existing_env_data_dir_wins(cli: CliFn, hub: FakeHub, clone: Path, tmp_path: Path) -> None:
    paths = Paths.resolve()
    paths.config_dir.mkdir(parents=True)
    custom = tmp_path / "custom-data"
    paths.env_file.write_text(f'DATA_DIR="{custom}"\n')
    assert _setup(cli, clone, "--token-file", str(tmp_path / "a.key"), "--no-open")[0] == 0
    assert (custom / "wispralt.db").exists()
    state = InstallState.load(paths.state_file)
    assert state is not None and state.paths["data_dir"] == str(custom)
    code, _, err = _setup(cli, clone, "--data-dir", str(tmp_path / "other"), "--no-open")
    assert code == 2 and "disagrees" in err


def test_unhealthy_service_fails_after_the_key_is_handed_over(
    cli: CliFn, hub: FakeHub, clone: Path, tmp_path: Path, http: FakeHttp
) -> None:
    http.default = None
    tok = tmp_path / "a.key"
    code, _, err = _setup(cli, clone, "--token-file", str(tok), "--no-open")
    assert code == 1 and "server.err.log" in err
    assert tok.exists()  # minted before the health wait, so it is never lost
