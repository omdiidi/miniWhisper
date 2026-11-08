"""
CLI test harness: a tmp ``HOME`` + ``WISPRALT_ROOT``, a test label, and fakes for
every OS seam. Any direct ``subprocess`` / ``os.execv`` call fails the test.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from wispralt.cli import host
from wispralt.cli import main as cli_main
from wispralt.cli.paths import Ctx, InstallState, Paths
from wispralt.cli.system import CliError, Completed, Deps, failure

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    CliFn = Callable[..., tuple[int, str, str]]

LABEL = "co.wispralt.test"
UID = os.getuid()
COMMIT = "c0ffee" + "0" * 34

_PRINT_RUNNING = """gui/{uid}/{label} = {{
\tactive count = 1
\tpath = /tmp/x.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /usr/bin/python3
\tpid = 4242
\tlast exit code = (never exited)
}}
"""


@dataclass
class Rule:
    prefix: tuple[str, ...]
    rc: int
    out: str
    err: str
    times: int | None


@dataclass
class FakeRunner:
    """Records argv; simulates launchctl load state; ``on()`` scripts other results."""

    calls: list[list[str]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    loaded: set[str] = field(default_factory=set)
    hooks: list[tuple[tuple[str, ...], Callable[[list[str]], None]]] = field(default_factory=list)

    def hook(self, *prefix: str, fn: Callable[[list[str]], None]) -> None:
        """Run *fn(argv)* as a side effect of a matching call (e.g. a build creating its app)."""
        self.hooks.append((prefix, fn))

    def on(
        self, *prefix: str, rc: int = 0, out: str = "", err: str = "", times: int | None = None
    ) -> None:
        self.rules.insert(0, Rule(prefix, rc, out, err, times))

    def run(
        self, argv: Sequence[str], *, input: str | None = None, check: bool = True
    ) -> Completed:
        argv = list(argv)
        self.calls.append(argv)
        self.inputs.append(input)
        for prefix, fn in self.hooks:
            if tuple(argv[: len(prefix)]) == prefix:
                fn(argv)
        if argv[0] == "ditto" and Path(argv[-2]).is_dir():  # simulate the copy
            shutil.copytree(argv[-2], argv[-1], symlinks=True)
        res = self._scripted(argv) or self._launchctl(argv) or Completed(argv, 0, "", "")
        if check and res.returncode != 0:
            raise CliError(1, failure(argv, res))
        return res

    def _scripted(self, argv: list[str]) -> Completed | None:
        for rule in self.rules:
            if tuple(argv[: len(rule.prefix)]) == rule.prefix and rule.times != 0:
                if rule.times is not None:
                    rule.times -= 1
                return Completed(argv, rule.rc, rule.out, rule.err)
        return None

    def _launchctl(self, argv: list[str]) -> Completed | None:
        if argv[:1] != ["launchctl"]:
            return None
        verb = argv[1]
        if verb == "print":
            label = argv[2].rsplit("/", 1)[-1]
            if label in self.loaded:
                return Completed(argv, 0, _PRINT_RUNNING.format(uid=UID, label=label), "")
            return Completed(argv, 113, "", f'Could not find service "{label}"')
        if verb == "bootstrap":
            self.loaded.add(argv[3].rsplit("/", 1)[-1].removesuffix(".plist"))
        elif verb == "bootout":
            self.loaded.discard(argv[2].rsplit("/", 1)[-1])
        return None

    def verbs(self, program: str = "launchctl") -> list[str]:
        return [c[1] for c in self.calls if c[0] == program and len(c) > 1]

    def argv_text(self) -> str:
        return "\n".join(" ".join(c) for c in self.calls)


@dataclass
class FakeHttp:
    status: dict[str, int | None] = field(default_factory=dict)
    default: int | None = 200
    seen: list[str] = field(default_factory=list)

    def __call__(self, url: str, timeout: float) -> int | None:
        self.seen.append(url)
        for suffix, code in self.status.items():
            if url.endswith(suffix):
                return code
        return self.default


@dataclass
class Execs:
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> None:
        self.calls.append(argv)
        raise SystemExit(0)  # like os.execv, the caller never continues


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("cli tests must go through the FakeRunner, not subprocess/execv")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "execv", _forbidden)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WISPRALT_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("WISPRALT_LABEL", LABEL)
    monkeypatch.delenv("HF_HOME", raising=False)  # main() pins it; restored after each test
    (tmp_path / "home").mkdir()


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def execs() -> Execs:
    return Execs()


@pytest.fixture
def answers() -> list[str]:
    """Scripted replies for interactive prompts (popped in order)."""
    return []


@pytest.fixture
def deps(runner: FakeRunner, http: FakeHttp, execs: Execs, answers: list[str]) -> Deps:
    return Deps(
        runner=runner,
        http_get=http,
        execv=execs,
        sleep=lambda _s: None,
        ask=lambda _q: answers.pop(0),
        stdin_isatty=False,
        stdout_isatty=False,
        machine="arm64",
        macos="15.4",
        which=lambda name: f"/opt/homebrew/bin/{name}",
        disk_free=lambda _p: 100 * 10**9,
    )


@pytest.fixture
def cli(deps: Deps, capsys: pytest.CaptureFixture[str]) -> CliFn:
    """``cli("status", "--json") -> (exit_code, stdout, stderr)`` in-process."""

    def _run(*argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        try:
            code = cli_main.main(list(argv), deps)
        except SystemExit as exc:
            code = int(exc.code or 0)
        out, err = capsys.readouterr()
        return code, out, err

    return _run


@pytest.fixture
def ctx(deps: Deps) -> Ctx:
    """An installed host: recorded state (default paths) and an existing data dir."""
    paths = Paths.resolve()
    state = InstallState(
        role="host",
        label=paths.label,
        paths={"data_dir": str(paths.data_dir), "backup_dir": str(paths.backup_dir)},
    )
    state.save(paths.state_file)
    paths.data_dir.mkdir(mode=0o700, parents=True)
    return Ctx(paths=paths, state=state, deps=deps)


@dataclass
class FakeHub:
    """Two tiny "models" per repo (an LFS file with a sha256, a small file checked by size)."""

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
    """Fake Hugging Face plus a fake Parakeet loader for the warm-up."""
    fake = FakeHub(tmp_path / "hub")
    deps.hub = fake
    deps.load_fn = lambda _id: (lambda _samples: SimpleNamespace(text=""))
    return fake


def info_plist(version: str = "0.5.0") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        f"<key>CFBundleShortVersionString</key><string>{version}</string></dict></plist>"
    )


def make_app(path: Path, version: str = "0.5.0") -> Path:
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    (path / "Contents" / "Info.plist").write_text(info_plist(version))
    return path


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A minimal WisprAlt clone: server/pyproject.toml and the client Info.plist."""
    src = tmp_path / "clone"
    (src / "server").mkdir(parents=True)
    (src / "server" / "pyproject.toml").write_text("[project]\nname='wispralt'\n")
    (src / "client" / "WisprAlt").mkdir(parents=True)
    (src / "client" / "WisprAlt" / "Info.plist").write_text(info_plist())
    return src
