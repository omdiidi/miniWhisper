"""
setup_code.py — the one-time code that unlocks ``/admin/setup`` (portal first-admin setup).

While no enabled admin exists, the server writes ``data_dir/setup-code`` (0600, 128 bits)
at startup and logs only its path. Pasting the file's content into ``/admin/setup`` proves
local filesystem access: cloudflared proxies from loopback, so "localhost only" would be
no boundary. A successful setup deletes the file. The CLI bootstrap stays the primary path;
both call ``accounts.service.bootstrap_first_admin``.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore

logger = logging.getLogger(__name__)

FILENAME = "setup-code"


def code_path(data_dir: Path) -> Path:
    return Path(data_dir) / FILENAME


def write(data_dir: Path) -> Path:
    """Write a fresh code (0600, replacing any older one) and return its path."""
    path = code_path(data_dir)
    path.unlink(missing_ok=True)  # a fresh inode, so the mode below always applies
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(16) + "\n")
    return path


def matches(data_dir: Path, candidate: str) -> bool:
    """Constant-time compare of *candidate* with the code on disk. False if there is none."""
    try:
        stored = code_path(data_dir).read_text().strip()
    except FileNotFoundError:
        return False
    return bool(stored) and hmac.compare_digest(stored.encode(), candidate.strip().encode())


def consume(data_dir: Path) -> None:
    code_path(data_dir).unlink(missing_ok=True)


def prepare(accounts: AccountStore, data_dir: Path) -> Path | None:
    """Startup hook: write the code when no enabled admin exists, else remove a stale one."""
    if accounts.has_enabled_admin():
        consume(data_dir)
        return None
    path = write(data_dir)
    logger.warning(
        "No admin account yet. Run the CLI bootstrap, or open /admin/setup and paste the code"
        " from %s",
        path,
    )
    return path
