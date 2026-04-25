"""
ops/env_writer.py — Atomic, permission-preserving .env file rewriter.

v3 delta Task 7.5: used by ``POST /admin/rotate-key`` to update
WISPRALT_API_KEY in .env without a server restart and without ever
leaving a partially-written file on disk.

Security guarantees:
- The temp file is created with os.open / O_CREAT | O_EXCL (NamedTemporaryFile
  default) so no other process can race to read a partially-written value.
- ``os.chmod(tmp, 0o600)`` is called BEFORE ``os.replace`` so the destination
  file never exists with permissive mode, even momentarily.
- Both source and destination must be on the same filesystem for ``os.replace``
  to be atomic (POSIX rename(2)).  The caller is responsible for ensuring this.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def rewrite_env_var(path: Path, key: str, value: str) -> None:
    """Atomically rewrite or append a key in a .env file.

    Algorithm
    ---------
    1. Read all lines from *path* (create empty if missing).
    2. Replace the line matching ``^KEY=...`` with ``KEY=value``.
       If no such line exists, append it.
    3. Write to a NamedTemporaryFile in the same directory (same filesystem).
    4. ``os.chmod(tmp, 0o600)`` BEFORE ``os.replace`` — destination is never
       readable by others, even transiently.
    5. ``os.replace(tmp, path)`` — atomic rename on POSIX/APFS.

    Parameters
    ----------
    path:
        Absolute path to the .env file.
    key:
        Environment variable name (e.g. ``"WISPRALT_API_KEY"``).
    value:
        New value (plain string, not shell-escaped here; caller must not embed
        shell metacharacters unless they intend them to be literal).
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"

    # Read existing content (if the file does not exist yet, start empty)
    if path.exists():
        original_lines = path.read_text(encoding="utf-8")
    else:
        original_lines = ""

    if pattern.search(original_lines):
        new_content = pattern.sub(replacement, original_lines)
    else:
        # Append; ensure there is exactly one trailing newline before the new line
        new_content = original_lines.rstrip("\n") + ("\n" if original_lines else "") + replacement + "\n"

    # Write atomically: temp file in same directory → chmod 600 → rename
    #
    # os.fdopen() takes ownership of tmp_fd; after that call the file object's
    # context manager is the sole owner of the fd.  We must NOT call os.close()
    # on tmp_fd once os.fdopen() has returned successfully, because the "with"
    # block's __exit__ will close it — doing so twice is undefined behaviour and
    # can silently close an unrelated fd that was re-allocated in between.
    #
    # Error-handling strategy:
    #   • If os.fdopen() itself raises (extremely unlikely), tmp_fd is still open
    #     and we fall through to the outer except which only calls os.unlink().
    #     The fd leaks for the process lifetime, which is acceptable for this
    #     exceptional path.
    #   • If the write raises inside the "with" block, __exit__ closes the fd
    #     cleanly; the outer except then removes the (possibly partial) temp file.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)

        # CRITICAL: set permissions BEFORE making the file visible at final path
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp on any failure so we don't leave stale files.
        # Do NOT call os.close(tmp_fd) here — os.fdopen() already transferred
        # ownership to the file object, which __exit__ has already closed.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.debug("Rewrote %s in %s", key, path)


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

    This is a convenience replica of the check in ``config.verify_env_perms``;
    having it here lets ``env_writer`` be tested in isolation.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("Could not stat %s: %s", path, exc)
        return False

    mode = stat.S_IMODE(st.st_mode)
    return st.st_uid == os.getuid() and mode == 0o600
