"""
importer/lock.py — the offline lock a real (non-dry) import holds.

MERGE SEAM: Part 4 Task 7 adds ``wispralt.ops.backup.server_lock`` (the same
``fcntl.flock(LOCK_EX | LOCK_NB)`` on the sibling ``<data_dir>.lock`` that the
server lifespan holds). When it lands, replace this body with
``from wispralt.ops.backup import server_lock as offline_lock``; the importer
only calls ``offline_lock(data_dir)``.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from wispralt._errors import ServerRunning

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def offline_lock(data_dir: Path) -> Iterator[None]:
    lock_path = data_dir.with_name(data_dir.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServerRunning(f"{lock_path} is held: stop the server first") from exc
        yield
    finally:
        os.close(fd)
