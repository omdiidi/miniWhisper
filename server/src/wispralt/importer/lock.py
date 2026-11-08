"""
importer/lock.py — ``offline(data_dir)``: the server lock, re-entrant within one import.

``ops.backup.server_lock`` is an ``flock`` on a fresh descriptor, so a second
acquire in the same process would refuse. The CLI holds ``offline`` around
migrate + apply; the apply functions take it again and get a no-op, while a
library caller on its own still gets the real lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from wispralt.ops.backup import server_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

_held: ContextVar[frozenset[Path]] = ContextVar("wispralt_import_locks", default=frozenset())


@contextmanager
def offline(data_dir: Path) -> Iterator[None]:
    """Hold ``server_lock(data_dir)`` (``ServerRunning`` if a server has it); nests."""
    key = Path(data_dir).resolve()
    if key in _held.get():
        yield
        return
    with server_lock(key):
        token = _held.set(_held.get() | {key})
        try:
            yield
        finally:
            _held.reset(token)
