"""
cli/system.py — the CLI's only seam to the operating system.

Every external program (``launchctl``, ``security``, ``defaults``, ``open``,
``uv``, ``git``, ``ditto``, ``codesign``, ``spctl``, ``hdiutil`` …) runs through
``Runner.run``; HTTP probes go through ``http_get``; process replacement through
``execv``. ``Deps`` bundles them so tests swap in fakes and nothing real runs.
No other ``cli`` module calls ``subprocess`` or ``os.execv``.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_DOCTOR = 0, 1, 2, 3


class CliError(Exception):
    """A command failure with its exit code (1 failed, 2 usage / missing answer)."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


Completed = subprocess.CompletedProcess[str]


class RunnerLike(Protocol):
    def run(
        self, argv: Sequence[str], *, input: str | None = None, check: bool = True
    ) -> Completed: ...


class Runner:
    """Runs a program with captured text output. Never pass a secret in *argv*."""

    def run(
        self, argv: Sequence[str], *, input: str | None = None, check: bool = True
    ) -> Completed:
        try:
            res = subprocess.run(
                list(argv), input=input, capture_output=True, text=True, check=False
            )
        except FileNotFoundError as exc:
            raise CliError(1, f"{argv[0]} not found") from exc
        if check and res.returncode != 0:
            raise CliError(1, failure(argv, res))
        return res


def failure(argv: Sequence[str], res: Completed) -> str:
    """One line naming the command (program + subcommand) and its last stderr line."""
    last = (res.stderr or res.stdout or "").strip().splitlines()
    tail = f": {last[-1]}" if last else ""
    return f"`{' '.join(argv[:2])}` failed (exit {res.returncode}){tail}"


def http_get(url: str, timeout: float) -> int | None:
    """HTTP status of a GET, or None when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (loopback URL)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def version() -> str:
    try:
        return metadata.version("wispralt")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def _disk_free(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _execv(argv: list[str]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)


@dataclass(slots=True)
class Deps:
    """Everything a command touches outside Python; tests replace each field."""

    runner: RunnerLike = field(default_factory=Runner)
    http_get: Callable[[str, float], int | None] = http_get
    execv: Callable[[list[str]], None] = _execv
    sleep: Callable[[float], None] = time.sleep
    ask: Callable[[str], str] = input
    stdin_isatty: bool = field(default_factory=lambda: sys.stdin.isatty())
    stdout_isatty: bool = field(default_factory=lambda: sys.stdout.isatty())
    machine: str = field(default_factory=platform.machine)
    macos: str = field(default_factory=lambda: platform.mac_ver()[0])
    which: Callable[[str], str | None] = shutil.which
    disk_free: Callable[[Path], int] = _disk_free
    load_fn: Any = None  # Parakeet ``LoadFn`` override for the warm-up (tests)
    hub: Any = None  # ``host.Hub``; None → Hugging Face (``host.HfHub``)
