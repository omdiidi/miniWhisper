"""
cli/paths.py — where a WisprAlt install lives, and what it recorded.

``Paths`` resolves every location from ``WISPRALT_ROOT`` (default ``$HOME``) so
an E2E run can live in a throwaway root. LaunchAgents are the exception: launchd
only autoloads ``$HOME/Library/LaunchAgents``, so the plist always goes there
(tests isolate it by pointing ``HOME`` at a tmp dir and ``WISPRALT_LABEL`` at a
test label).

``InstallState`` is ``install-state.json`` in the config dir: what setup chose
and created. ``artifacts`` are removed by a plain ``uninstall``; ``data`` only by
``--purge-data`` (backups never). Writes are atomic and 0600.

``merge_missing`` adds absent keys to the host ``.env`` and never rewrites an
existing line, so a rerun of setup keeps every hand-edited value.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from wispralt.cli.system import CliError

if TYPE_CHECKING:
    from wispralt.cli.system import Deps, RunnerLike

ROOT_ENV = "WISPRALT_ROOT"
LABEL_ENV = "WISPRALT_LABEL"
DEFAULT_LABEL = "co.wispralt.host"
LEGACY_LABEL = "co.wispralt.server"
DEFAULT_PORT = 8000
APP_NAME = "WisprAlt.app"
DB_NAME = "wispralt.db"  # mirrors ops.backup.DB_NAME / Settings.db_path

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path
    home: Path
    label: str
    data_dir: Path
    backup_dir: Path
    rerooted: bool = False

    @classmethod
    def resolve(cls, *, data_dir: Path | None = None, backup_dir: Path | None = None) -> Paths:
        """Locations for this process; *data_dir*/*backup_dir* come from flags or state."""
        home = Path.home()
        root = Path(os.environ.get(ROOT_ENV) or home).expanduser().absolute()
        support = root / "Library" / "Application Support" / "WisprAlt"
        return cls(
            root=root,
            home=home,
            label=os.environ.get(LABEL_ENV) or DEFAULT_LABEL,
            data_dir=_abs(data_dir) if data_dir else support / "host",
            backup_dir=_abs(backup_dir) if backup_dir else root / "WisprAlt-Backups",
            rerooted=bool(os.environ.get(ROOT_ENV)),
        )

    @property
    def config_dir(self) -> Path:
        return self.root / ".config" / "wispralt"

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def state_file(self) -> Path:
        return self.config_dir / "install-state.json"

    @property
    def models_dir(self) -> Path:
        """``HF_HOME`` for the host's model weights."""
        return self.root / "Library" / "Application Support" / "WisprAlt" / "models"

    @property
    def logs_dir(self) -> Path:
        return self.root / "Library" / "Logs" / "WisprAlt"

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    @property
    def app(self) -> Path:
        """The client app; a re-rooted (E2E) install never touches /Applications."""
        return (self.root / "Applications" if self.rerooted else Path("/Applications")) / APP_NAME

    @property
    def launch_agents(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def plist(self) -> Path:
        return self.launch_agents / f"{self.label}.plist"

    @property
    def legacy_plist(self) -> Path:
        return self.launch_agents / f"{LEGACY_LABEL}.plist"


def _abs(path: Path) -> Path:
    return Path(path).expanduser().absolute()


@dataclass(slots=True)
class InstallState:
    version: str = ""
    source_dir: str | None = None
    role: str | None = None
    label: str = DEFAULT_LABEL
    port: int = DEFAULT_PORT
    paths: dict[str, str] = field(default_factory=dict[str, str])
    models: dict[str, str] = field(default_factory=dict[str, str])
    artifacts: list[str] = field(default_factory=list[str])
    data: list[str] = field(default_factory=list[str])
    retention_prompted: bool = False
    last_run: float | None = None

    @classmethod
    def load(cls, path: Path) -> InstallState | None:
        """The recorded state, or None when there is no install yet."""
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CliError(1, f"unreadable {path}: {type(exc).__name__}") from exc
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        """Atomic write (tmp + ``os.replace``), 0600; a crash leaves the old file intact."""
        self.last_run = time.time()
        write_private(path, json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    def add(self, bucket: str, *items: Path | str) -> None:
        """Record created paths under ``artifacts`` or ``data`` (no duplicates)."""
        target: list[str] = getattr(self, bucket)
        for item in items:
            if str(item) not in target:
                target.append(str(item))


@dataclass(slots=True)
class Ctx:
    """What every command handler gets: locations, recorded state, OS seams, flags."""

    paths: Paths
    state: InstallState | None
    deps: Deps
    interactive: bool = False
    yes: bool = False
    json: bool = False
    argv: list[str] = field(default_factory=list[str])

    @property
    def runner(self) -> RunnerLike:
        return self.deps.runner

    @property
    def port(self) -> int:
        return self.state.port if self.state else DEFAULT_PORT

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def need_state(self) -> InstallState:
        if self.state is None:
            raise CliError(
                1, f"no install recorded at {self.paths.state_file}; run `wispralt setup`"
            )
        return self.state

    def confirm(self, question: str) -> bool:
        """``--yes``, else an interactive y/N; non-interactive without ``--yes`` → exit 2."""
        if self.yes:
            return True
        if not self.interactive:
            raise CliError(2, f"{question} — pass --yes to confirm non-interactively")
        return self.deps.ask(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_private(path: Path, text: str) -> None:
    """Replace *path* atomically with *text*, mode 0600."""
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    path.chmod(0o600)


def env_keys(path: Path) -> set[str]:
    """Keys assigned in a dotenv file (values are never read here)."""
    if not path.exists():
        return set()
    return {m.group(1) for line in path.read_text().splitlines() if (m := _KEY_RE.match(line))}


def env_value(path: Path, key: str) -> str | None:
    """One value from a dotenv file (python-dotenv's parser), or None."""
    return dotenv_values(path).get(key) if path.exists() else None


def merge_missing(path: Path, values: dict[str, str]) -> list[str]:
    """Append each key of *values* absent from *path*; returns the keys added. 0600."""
    present = env_keys(path)
    added = [k for k in values if k not in present]
    for k in added:
        if any(c in values[k] for c in '"\\\n'):
            raise CliError(2, f"{k} value may not contain quotes, backslashes or newlines")
    if not added and path.exists():
        path.chmod(0o600)  # untouched otherwise: a rerun changes no byte
        return added
    old = path.read_text() if path.exists() else ""
    sep = "" if not old or old.endswith("\n") else "\n"
    write_private(path, old + sep + "".join(f'{k}="{values[k]}"\n' for k in added))
    return added
