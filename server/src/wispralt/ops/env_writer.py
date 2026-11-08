"""
ops/env_writer.py — locate the server ``.env`` and check its permissions.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def find_env_path() -> Path:
    """Locate the .env file used by the server.

    Search order:
    1. ``server/.env`` relative to CWD (typical when started from repo root).
    2. ``.env`` in CWD (typical when started from the server/ directory).
    3. Fall back to CWD/.env (let callers create it if needed).

    M4: Single canonical implementation; used by both main.py and routes/admin.py
    to avoid the duplicated _locate_env / _find_env_path functions.
    """
    candidates = [
        Path.cwd() / "server" / ".env",
        Path.cwd() / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path.cwd() / ".env"


def verify_env_perms(path: Path) -> bool:
    """Return True iff *path* is mode 0600 and owned by the current user.

    Logs SECURITY WARNING messages if the file is accessible to other users so
    the server can still start in CI/container environments where the .env is
    intentionally world-readable via secret injection.

    This is the single canonical implementation; ``config.py`` re-exports it.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        # No .env on disk is fine — values may come from the real environment.
        return True
    except OSError as exc:
        logger.warning("Could not stat %s: %s", path, exc)
        return False

    mode = stat.S_IMODE(st.st_mode)
    owner_ok = st.st_uid == os.getuid()
    mode_ok = mode == 0o600

    if not owner_ok:
        logger.warning(
            "SECURITY WARNING: %s is owned by uid %d, but current uid is %d. "
            "This means other users on the system may read your secrets. "
            "Fix with: sudo chown $(whoami) %s",
            path,
            st.st_uid,
            os.getuid(),
            path,
        )
    if not mode_ok:
        logger.warning(
            "SECURITY WARNING: %s has mode %o, expected 0600. "
            "Other processes may be able to read your API key and other secrets. "
            "Fix with: chmod 600 %s",
            path,
            mode,
            path,
        )

    return owner_ok and mode_ok
