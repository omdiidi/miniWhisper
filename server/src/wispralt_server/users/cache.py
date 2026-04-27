"""
users/cache.py — 60s in-process LRU cache for sha256(token) → :class:`User`.

The cache is keyed by token_hash (never by the plaintext bearer).  Entries
expire after 60 seconds so that revocations propagate within a bounded
window even when the admin UI revoke handler forgets to invalidate
explicitly.  Capacity is 256, evicting the least-recently-used entry on
overflow.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import User


class TokenCache:
    """LRU cache keyed by token_hash. 60s TTL, max 256 entries."""

    _MAX = 256
    _TTL_S = 60.0

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[User, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, token_hash: str) -> User | None:
        """Return the cached user or ``None`` (miss / expired)."""
        with self._lock:
            entry = self._items.get(token_hash)
            if entry is None:
                return None
            user, ts = entry
            if time.monotonic() - ts > self._TTL_S:
                del self._items[token_hash]
                return None
            self._items.move_to_end(token_hash)  # LRU touch
            return user

    def put(self, token_hash: str, user: User) -> None:
        """Insert or refresh a cache entry, evicting the LRU on overflow."""
        with self._lock:
            self._items[token_hash] = (user, time.monotonic())
            self._items.move_to_end(token_hash)
            while len(self._items) > self._MAX:
                self._items.popitem(last=False)

    def invalidate(self, token_hash: str) -> None:
        """Remove a single entry (no-op if absent)."""
        with self._lock:
            self._items.pop(token_hash, None)
