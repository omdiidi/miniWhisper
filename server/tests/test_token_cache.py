"""
test_token_cache.py — Coverage for the in-process :class:`TokenCache` LRU + TTL.

Background
----------
``TokenCache`` is the auth fast-path that keeps Postgres off the dictation
hot path.  Two invariants must hold or the auth layer either over-caches
(stale revokes leak through) or under-caches (every request hits Postgres):

    1. Entries expire ``_TTL_S`` seconds after insertion.
    2. The cache evicts the LRU entry once ``_MAX`` entries are stored.

These tests pin both with a monkey-patched ``time.monotonic`` so the TTL
boundary is deterministic.
"""

from __future__ import annotations

from wispralt_server.users.cache import TokenCache
from wispralt_server.users.store import User


def _make_user(uid: int) -> User:
    return User(id=uid, label=f"user-{uid}", role="employee")


class TestPutAndGet:
    """Basic round-trip behavior."""

    def test_put_then_get_returns_same_user(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        user = _make_user(1)
        cache.put("hash-A", user)
        assert cache.get("hash-A") == user

    def test_get_unknown_hash_returns_none(self) -> None:
        cache = TokenCache()
        assert cache.get("never-inserted") is None


class TestTTL:
    """Entries must expire after ``_TTL_S`` seconds of monotonic time."""

    def test_get_after_ttl_returns_none(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        cache.put("hash-A", _make_user(1))

        # Just inside the TTL window — still hits.
        fake_now[0] = 1000.0 + cache._TTL_S - 0.01
        assert cache.get("hash-A") is not None

        # Just outside — must evict and return None.
        fake_now[0] = 1000.0 + cache._TTL_S + 0.01
        assert cache.get("hash-A") is None

    def test_expired_entry_is_removed_from_storage(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # After an expired ``get`` the underlying OrderedDict should no
        # longer contain the key — this is what lets the LRU bookkeeping
        # stay tight for long-lived processes.
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        cache.put("hash-A", _make_user(1))
        fake_now[0] = 1000.0 + cache._TTL_S + 1.0
        assert cache.get("hash-A") is None
        # Internal state — testing that the eviction physically dropped the entry.
        assert "hash-A" not in cache._items


class TestLRUEviction:
    """LRU must drop the *least recently used* entry, not the most recent."""

    def test_overflow_evicts_oldest(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        # Fill exactly to capacity + 1; oldest should be evicted on the
        # last put().
        for i in range(cache._MAX + 1):
            cache.put(f"hash-{i}", _make_user(i))

        # The very first inserted hash should be gone.
        assert cache.get("hash-0") is None
        # The most recently inserted hash should still be present.
        assert cache.get(f"hash-{cache._MAX}") is not None
        # And size is exactly _MAX.
        assert len(cache._items) == cache._MAX

    def test_get_marks_as_recent(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # An entry that was just `get`-touched should survive eviction
        # in favor of an older, untouched entry.
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        # Fill to capacity.
        for i in range(cache._MAX):
            cache.put(f"hash-{i}", _make_user(i))

        # Touch the oldest entry — it should now be the most recently used.
        assert cache.get("hash-0") is not None
        # Adding one more should evict the now-second-oldest, NOT hash-0.
        cache.put("hash-NEW", _make_user(9999))
        assert cache.get("hash-0") is not None, "LRU touch did not protect hash-0"
        assert cache.get("hash-1") is None, "expected hash-1 to be evicted"


class TestInvalidate:
    """``invalidate`` must remove a single entry without touching others."""

    def test_invalidate_removes_entry(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        fake_now = [1000.0]
        monkeypatch.setattr(
            "wispralt_server.users.cache.time.monotonic", lambda: fake_now[0]
        )
        cache = TokenCache()
        cache.put("hash-A", _make_user(1))
        cache.put("hash-B", _make_user(2))
        cache.invalidate("hash-A")
        assert cache.get("hash-A") is None
        assert cache.get("hash-B") is not None

    def test_invalidate_missing_is_noop(self) -> None:
        # Must not raise — used by the revoke handler which doesn't know
        # whether the cache had a hit at all.
        cache = TokenCache()
        cache.invalidate("never-existed")  # no exception expected
