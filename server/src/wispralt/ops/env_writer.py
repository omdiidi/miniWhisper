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
    """The dotenv file ``Settings`` reads: ``.env`` in the working directory.

    ``Settings`` (``config.py``) loads ``env_file=".env"`` relative to the cwd,
    and real environment variables override it. The host LaunchAgent runs with
    ``WorkingDirectory`` = the config dir (``cli/launchd.py``), so for an
    installed host this is ``~/.config/wispralt/.env`` (``cli.paths.Paths.env_file``);
    a dev run reads ``./.env`` from wherever it starts. Only that one file is
    permission-checked: any other ``.env`` is never loaded.
    """
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
