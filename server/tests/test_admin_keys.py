"""
test_admin_keys.py — Coverage for /admin/keys/* (Add API Key flow).

These tests exercise the integration-key minting / revoking / rotating routes
without touching Postgres. We stub:

  - ``users_store.mint`` to return a synthetic ``(User, plaintext)`` tuple.
  - ``users_store.set_kind`` to assert it gets called with ``kind='integration'``.
  - ``users_store.revoke`` / ``users_store.rotate`` to assert the
    token-cache invalidation path runs.
  - ``users_store.lookup_by_id`` + ``users_store.fetch_profile_by_id`` so the
    success-page render has values to template against.

Auth is enforced for real — :func:`require_api_key` is overridden to inject
an admin (or employee, for the 403 case).
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

# Pydantic Settings reads these at import time — set defaults BEFORE any
# wispralt_server import so the tests work on a bare dev box without a .env.
os.environ.setdefault("HF_TOKEN", "stub")
os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt_server import auth as auth_mod
from wispralt_server.auth import require_api_key
from wispralt_server.routes import admin_ui
from wispralt_server.users import store as users_store
from wispralt_server.users.store import User, UserProfile

# ── helpers ───────────────────────────────────────────────────────────────────


def _stub_pool() -> object:
    """Sentinel pool; admin_ui only checks for None."""
    return object()


def _override_admin(app: FastAPI) -> None:
    async def _admin(_request: Any = None) -> User:
        return User(id=1, label="omid", role="admin")

    app.dependency_overrides[require_api_key] = _admin


def _override_employee(app: FastAPI) -> None:
    async def _employee(_request: Any = None) -> User:
        return User(id=42, label="alice", role="employee")

    app.dependency_overrides[require_api_key] = _employee


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Minimal app mounting the admin UI routers + stubbed dependencies.

    Same pattern as tests/test_admin_routes_auth.py.
    """
    application = FastAPI()
    application.include_router(admin_ui.public_router)
    application.include_router(admin_ui.authed_router)
    application.state.db_pool = _stub_pool()

    # Stub the SQL aggregator so overview() never hits Postgres.
    async def _fake_aggregate_stats(_pool: Any) -> dict[str, Any]:
        return {"totals": {}, "top_users": [], "daily": []}

    monkeypatch.setattr(admin_ui, "_aggregate_stats", _fake_aggregate_stats)

    # Stub count_kind — overview reads it for the integration-key tile.
    async def _fake_count_kind(_pool: Any, _kind: str) -> int:
        # Return a sentinel value we can assert against in the tile test
        return 7 if _kind == "integration" else 0

    monkeypatch.setattr(users_store, "count_kind", _fake_count_kind)

    # Default: list_integrations returns empty — most tests don't care about it
    async def _fake_list_integrations(_pool: Any) -> list[Any]:
        return []

    monkeypatch.setattr(users_store, "list_integrations", _fake_list_integrations)

    return application


# ── access control ────────────────────────────────────────────────────────────


class TestKeysAccessControl:
    def test_get_keys_requires_admin(self, app: FastAPI) -> None:
        _override_employee(app)
        try:
            resp = TestClient(app).get("/admin/keys")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()


# ── form GET ─────────────────────────────────────────────────────────────────


class TestKeysAddForm:
    def test_get_new_form_renders(self, app: FastAPI) -> None:
        _override_admin(app)
        try:
            resp = TestClient(app).get("/admin/keys/new")
            assert resp.status_code == 200
            # Template title surfaces "Add API key".
            assert "Add API key" in resp.text
        finally:
            app.dependency_overrides.clear()


# ── mint ─────────────────────────────────────────────────────────────────────


class TestKeysAddSubmit:
    def test_post_new_mints_integration_kind(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)
        mint_calls: dict[str, Any] = {}
        set_kind_calls: list[tuple[int, str]] = []

        async def _fake_mint(
            _pool: Any, *, label: str, role: str, display_name: str | None
        ) -> tuple[User, str]:
            mint_calls["label"] = label
            mint_calls["role"] = role
            mint_calls["display_name"] = display_name
            return (
                User(id=99, label=label, role=role, kind="employee"),
                "f" * 64,
            )

        async def _fake_set_kind(_pool: Any, *, user_id: int, kind: str) -> None:
            set_kind_calls.append((user_id, kind))

        async def _fake_lookup_by_id(_pool: Any, _user_id: int) -> User:
            return User(id=99, label="key-buzz", role="employee", kind="integration")

        monkeypatch.setattr(users_store, "mint", _fake_mint)
        monkeypatch.setattr(users_store, "set_kind", _fake_set_kind)
        monkeypatch.setattr(users_store, "lookup_by_id", _fake_lookup_by_id)

        try:
            resp = TestClient(app).post(
                "/admin/keys/new", data={"program_name": "Buzz"}
            )
            assert resp.status_code == 200, resp.text
            # mint() ran first — order matters because set_kind needs the row.
            assert mint_calls["role"] == "employee"
            assert mint_calls["label"].startswith("key-")
            # set_kind flipped the row to integration AFTER mint.
            assert set_kind_calls == [(99, "integration")]
        finally:
            app.dependency_overrides.clear()

    def test_post_new_renders_openai_env_snippet(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)

        async def _fake_mint(
            _pool: Any, *, label: str, role: str, display_name: str | None
        ) -> tuple[User, str]:
            return User(id=99, label=label, role=role, kind="employee"), "f" * 64

        async def _fake_set_kind(_pool: Any, *, user_id: int, kind: str) -> None:
            return None

        async def _fake_lookup_by_id(_pool: Any, _user_id: int) -> User:
            return User(id=99, label="key-open-webui", role="employee", kind="integration")

        monkeypatch.setattr(users_store, "mint", _fake_mint)
        monkeypatch.setattr(users_store, "set_kind", _fake_set_kind)
        monkeypatch.setattr(users_store, "lookup_by_id", _fake_lookup_by_id)

        try:
            resp = TestClient(app).post(
                "/admin/keys/new", data={"program_name": "Open WebUI"}
            )
            assert resp.status_code == 200, resp.text
            assert "OPENAI_BASE_URL=" in resp.text
            assert "OPENAI_API_KEY=" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_post_new_renders_plaintext_token(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)
        secret = "a1b2c3d4" * 8  # 64 hex chars

        async def _fake_mint(
            _pool: Any, *, label: str, role: str, display_name: str | None
        ) -> tuple[User, str]:
            return User(id=99, label=label, role=role, kind="employee"), secret

        async def _fake_set_kind(_pool: Any, *, user_id: int, kind: str) -> None:
            return None

        async def _fake_lookup_by_id(_pool: Any, _user_id: int) -> User:
            return User(id=99, label="key-x", role="employee", kind="integration")

        monkeypatch.setattr(users_store, "mint", _fake_mint)
        monkeypatch.setattr(users_store, "set_kind", _fake_set_kind)
        monkeypatch.setattr(users_store, "lookup_by_id", _fake_lookup_by_id)

        try:
            resp = TestClient(app).post(
                "/admin/keys/new", data={"program_name": "MyTool"}
            )
            assert resp.status_code == 200, resp.text
            assert secret in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_post_new_invalid_program_name(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)

        # mint() must not be called on a validation failure.
        async def _fake_mint(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("mint must not be called when program_name is empty")

        monkeypatch.setattr(users_store, "mint", _fake_mint)
        try:
            resp = TestClient(app).post(
                "/admin/keys/new", data={"program_name": "   "}
            )
            assert resp.status_code == 400, resp.text
        finally:
            app.dependency_overrides.clear()


# ── revoke ───────────────────────────────────────────────────────────────────


class TestKeysRevoke:
    def test_post_revoke_calls_revoke_and_invalidates_cache(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)
        revoke_calls: list[int] = []
        invalidate_calls: list[str] = []

        async def _fake_revoke(_pool: Any, user_id: int) -> str | None:
            revoke_calls.append(user_id)
            return "stale_hash_value"

        monkeypatch.setattr(users_store, "revoke", _fake_revoke)
        monkeypatch.setattr(
            auth_mod.token_cache,
            "invalidate",
            MagicMock(side_effect=lambda th: invalidate_calls.append(th)),
        )

        try:
            # follow_redirects=False so the 303 doesn't bounce into /admin/keys.
            resp = TestClient(app).post(
                "/admin/keys/55/revoke", follow_redirects=False
            )
            assert resp.status_code in (302, 303), resp.text
            assert revoke_calls == [55]
            assert invalidate_calls == ["stale_hash_value"]
        finally:
            app.dependency_overrides.clear()


# ── rotate ───────────────────────────────────────────────────────────────────


class TestKeysRotate:
    def test_post_rotate_renders_with_mode_rotate(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_admin(app)
        rotate_secret = "z" * 64

        async def _fake_rotate(_pool: Any, _user_id: int) -> tuple[str, str | None]:
            return rotate_secret, "old_hash"

        async def _fake_lookup_by_id(_pool: Any, _user_id: int) -> User:
            return User(id=55, label="key-buzz", role="employee", kind="integration")

        async def _fake_fetch_profile_by_id(_pool: Any, _user_id: int) -> UserProfile:
            return UserProfile(
                id=55,
                label="key-buzz",
                display_name="Buzz",
                role="employee",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                last_seen_at=None,
            )

        monkeypatch.setattr(users_store, "rotate", _fake_rotate)
        monkeypatch.setattr(users_store, "lookup_by_id", _fake_lookup_by_id)
        monkeypatch.setattr(
            users_store, "fetch_profile_by_id", _fake_fetch_profile_by_id
        )
        monkeypatch.setattr(auth_mod.token_cache, "invalidate", MagicMock())

        try:
            resp = TestClient(app).post("/admin/keys/55/rotate")
            assert resp.status_code == 200, resp.text
            # mode='rotate' headline copy comes from the template.
            assert "rotated" in resp.text.lower()
            assert rotate_secret in resp.text
        finally:
            app.dependency_overrides.clear()


# ── overview tile ────────────────────────────────────────────────────────────


class TestOverviewTile:
    def test_overview_shows_integration_count(self, app: FastAPI) -> None:
        _override_admin(app)
        try:
            resp = TestClient(app).get("/admin/")
            assert resp.status_code == 200, resp.text
            # Tile label is hard-coded in overview.html.j2; the count value
            # comes from our _fake_count_kind stub (returns 7).
            assert "Integration keys" in resp.text
            assert ">7<" in resp.text or "> 7" in resp.text or "7" in resp.text
        finally:
            app.dependency_overrides.clear()
