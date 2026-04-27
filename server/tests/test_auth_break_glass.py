"""
test_auth_break_glass.py — Coverage for the break-glass admin path.

The break-glass path is what guarantees the operator never gets locked
out of their own server when Postgres is degraded.  The contract:

    1. ``app.state.db_pool is None`` → cache miss → check break-glass hash.
    2. If the bearer's sha256 matches ``app.state.break_glass_token_hash``,
       return ``User(id=-1, label='break-glass-admin', role='admin')``.
    3. Negative user id is the sentinel that tells the observability
       middleware to skip the usage-event enqueue (no FK violation).

If this regresses, an operator with a healthy machine but a flaky
Supabase connection cannot rotate keys, mint employees, or revoke
compromised tokens until Postgres comes back.  That's the worst kind of
outage.

These tests call ``require_api_key`` directly with a fake Request so we
can assert the user identity, status codes, and ``request.state.user``
side-effect without a TestClient.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from wispralt_server.auth import require_api_key
from wispralt_server.users.cache import TokenCache
from wispralt_server.users.store import User, hash_token


# ── fake Request helper ────────────────────────────────────────────────────────


def _make_request(
    *,
    bearer: str | None,
    db_pool: object | None,
    break_glass_token_hash: str | None,
    cookie: str | None = None,
) -> MagicMock:
    """Build a ``Request``-shaped mock that carries the inputs ``require_api_key`` reads.

    Real ``starlette.requests.Request`` is non-trivially constructed; we
    only need it to expose ``headers.getlist``, ``cookies.get``,
    ``app.state``, and ``state`` (the assignment target for the resolved
    user).  ``MagicMock`` plus ``SimpleNamespace`` for the state buckets
    is the cleanest setup.
    """
    request = MagicMock(spec_set=["headers", "cookies", "app", "state"])

    if bearer is not None:
        # Authorization: Bearer <token>
        headers_list = [f"Bearer {bearer}"]
    else:
        headers_list = []
    request.headers = MagicMock()
    request.headers.getlist = MagicMock(return_value=headers_list)

    request.cookies = MagicMock()
    request.cookies.get = MagicMock(return_value=cookie)

    request.app = MagicMock()
    request.app.state = SimpleNamespace(
        db_pool=db_pool,
        break_glass_token_hash=break_glass_token_hash,
    )
    # Mutable namespace so the auth code can do `request.state.user = ...`.
    request.state = SimpleNamespace()
    return request


@pytest.fixture(autouse=True)
def _clear_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the module-level token cache with a fresh instance per test.

    Otherwise residual entries from a prior test could short-circuit the
    Postgres / break-glass paths we want to exercise.
    """
    from wispralt_server import auth as auth_mod

    monkeypatch.setattr(auth_mod, "token_cache", TokenCache())


# ── break-glass with no pool ───────────────────────────────────────────────────


class TestBreakGlassNoPool:
    """Postgres degraded (``db_pool is None``) — break-glass must still work."""

    @pytest.mark.asyncio
    async def test_known_key_returns_admin_sentinel(self) -> None:
        bearer = "known-key"
        request = _make_request(
            bearer=bearer,
            db_pool=None,
            break_glass_token_hash=hash_token(bearer),
        )

        user = await require_api_key(request)

        assert isinstance(user, User)
        assert user.id == -1
        assert user.role == "admin"
        assert user.label == "break-glass-admin"

    @pytest.mark.asyncio
    async def test_resolved_user_attached_to_request_state(self) -> None:
        bearer = "known-key"
        request = _make_request(
            bearer=bearer,
            db_pool=None,
            break_glass_token_hash=hash_token(bearer),
        )

        user = await require_api_key(request)

        # Observability middleware reads request.state.user — verify the
        # auth dep wires it up.
        assert request.state.user is user

    @pytest.mark.asyncio
    async def test_unknown_key_with_no_pool_returns_503(self) -> None:
        # No pool AND the bearer doesn't match the break-glass hash —
        # the operator can't recover this without fixing Postgres or
        # re-setting the env var.  503 (not 401) makes the failure mode
        # observable in monitoring.
        request = _make_request(
            bearer="wrong-key",
            db_pool=None,
            break_glass_token_hash=hash_token("known-key"),
        )

        with pytest.raises(HTTPException) as excinfo:
            await require_api_key(request)
        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_no_break_glass_configured_with_no_pool_returns_503(self) -> None:
        # If lifespan failed before populating ``break_glass_token_hash``,
        # break-glass simply isn't available — auth must fail closed.
        request = _make_request(
            bearer="anything",
            db_pool=None,
            break_glass_token_hash=None,
        )

        with pytest.raises(HTTPException) as excinfo:
            await require_api_key(request)
        assert excinfo.value.status_code == 503


# ── miscellaneous safety cases ────────────────────────────────────────────────


class TestMissingBearer:
    """No header, no cookie → 401 (NOT 503, even when Postgres is down)."""

    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self) -> None:
        request = _make_request(
            bearer=None,
            db_pool=None,
            break_glass_token_hash=hash_token("known-key"),
        )
        with pytest.raises(HTTPException) as excinfo:
            await require_api_key(request)
        assert excinfo.value.status_code == 401


class TestBreakGlassDoesNotApplyWhenPostgresHealthy:
    """If the Postgres lookup succeeds, break-glass must NOT shadow it.

    The seeded admin row in ``wispralt.users`` shares the same hash as
    the env-var, so the path through Postgres returns a real user with
    ``id >= 0``.  This test asserts that — i.e. when the pool resolves
    the hash, we never fall through to the ``id=-1`` sentinel.
    """

    @pytest.mark.asyncio
    async def test_postgres_hit_takes_precedence_over_break_glass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bearer = "known-key"
        real_user = User(id=7, label="seeded-admin", role="admin")

        async def _fake_lookup(_pool: object, _token_hash: str) -> User | None:
            return real_user

        # Patch the store lookup to return a real user.
        from wispralt_server import auth as auth_mod

        monkeypatch.setattr(auth_mod._store_mod, "lookup", _fake_lookup)

        # Provide BOTH a non-None pool AND a matching break-glass hash —
        # if the auth code falls through to break-glass anyway, this test
        # would observe id=-1.
        request = _make_request(
            bearer=bearer,
            db_pool=object(),  # any non-None sentinel
            break_glass_token_hash=hash_token(bearer),
        )

        user = await require_api_key(request)
        assert user is real_user
        assert user.id == 7  # not the break-glass sentinel
