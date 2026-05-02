"""
test_admin_routes_auth.py — Coverage for ``/admin/*`` access control.

The admin UI mounts two routers under the same ``/admin`` prefix:

    - ``public_router`` exposes ``/admin/login`` (GET form, POST submit) —
      these MUST be reachable without auth, otherwise the operator has no
      way to acquire the session cookie that gates the rest.
    - ``authed_router`` covers everything else and is gated by
      ``Depends(require_admin)`` + ``Depends(_require_db_pool)``.

These tests pin both invariants WITHOUT exercising the underlying SQL —
``_aggregate_stats`` is monkey-patched to a stub so we test only the
auth boundary, not the analytics CTE.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt_server.auth import require_admin, require_api_key
from wispralt_server.routes import admin_ui
from wispralt_server.users.store import User


# ── fixtures ──────────────────────────────────────────────────────────────────


def _stub_pool() -> object:
    """A sentinel object — admin_ui only checks ``is None``."""
    return object()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a minimal FastAPI app mounting just the admin routers.

    Side effect: replaces ``admin_ui._aggregate_stats`` with a stub so
    the overview route never reaches Postgres.  The goal of this file
    is auth coverage; the SQL CTE has its own (manual) verification path.
    """
    application = FastAPI()
    application.include_router(admin_ui.public_router)
    application.include_router(admin_ui.authed_router)

    # Stash a non-None pool so ``_require_db_pool`` doesn't 503.
    application.state.db_pool = _stub_pool()

    # Stub the SQL aggregator — auth tests must not require live Postgres.
    async def _fake_aggregate_stats(_pool: Any) -> dict[str, Any]:
        return {"totals": {}, "top_users": [], "daily": []}

    monkeypatch.setattr(admin_ui, "_aggregate_stats", _fake_aggregate_stats)

    return application


# ── public router (login) ─────────────────────────────────────────────────────


class TestPublicLoginRouter:
    """``/admin/login`` must be reachable WITHOUT any authentication."""

    def test_get_login_returns_200_unauth(self, app: FastAPI) -> None:
        client = TestClient(app)
        resp = client.get("/admin/login")
        assert resp.status_code == 200
        # Login form is HTML; the exact body shape is template-controlled
        # but we can sanity-check that we got a non-empty response.
        assert "text/html" in resp.headers.get("content-type", "")
        assert len(resp.text) > 0


# ── authed router (overview) ──────────────────────────────────────────────────


class TestAuthedOverviewRoute:
    """``GET /admin/`` must enforce the admin-role gate."""

    def test_no_auth_returns_401(self, app: FastAPI) -> None:
        # No dependency override — the real ``require_api_key`` runs and
        # rejects with 401 because there's no Authorization header and no
        # cookie.
        client = TestClient(app)
        resp = client.get("/admin/")
        assert resp.status_code == 401

    def test_employee_role_returns_403(self, app: FastAPI) -> None:
        # Override require_api_key to inject an employee user.  The
        # downstream require_admin dependency will reject with 403.
        async def _employee(_request: Any = None) -> User:
            return User(id=42, label="alice", role="employee")

        app.dependency_overrides[require_api_key] = _employee
        try:
            client = TestClient(app)
            resp = client.get("/admin/")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_admin_role_returns_200_html(self, app: FastAPI) -> None:
        async def _admin(_request: Any = None) -> User:
            return User(id=1, label="omid", role="admin")

        app.dependency_overrides[require_api_key] = _admin
        try:
            client = TestClient(app)
            resp = client.get("/admin/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")
        finally:
            app.dependency_overrides.clear()

    def test_admin_with_no_db_pool_returns_503(self, app: FastAPI) -> None:
        # Even with a valid admin user, the overview route requires the
        # pool to be present.  Setting it to None should trigger 503 from
        # ``_require_db_pool``.
        async def _admin(_request: Any = None) -> User:
            return User(id=1, label="omid", role="admin")

        app.dependency_overrides[require_api_key] = _admin
        app.state.db_pool = None
        try:
            client = TestClient(app)
            resp = client.get("/admin/")
            assert resp.status_code == 503
        finally:
            app.dependency_overrides.clear()
            app.state.db_pool = _stub_pool()


class TestAddEmployeeRoute:
    """``GET /admin/users/new`` (form) and ``POST /admin/users/new`` (mint)."""

    @staticmethod
    def _override_admin(app: FastAPI) -> None:
        async def _admin(_request: Any = None) -> User:
            return User(id=1, label="omid", role="admin")

        app.dependency_overrides[require_api_key] = _admin

    def test_form_get_returns_200_for_admin(self, app: FastAPI) -> None:
        self._override_admin(app)
        try:
            resp = TestClient(app).get("/admin/users/new")
            assert resp.status_code == 200
            assert "Add employee" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_form_get_employee_returns_403(self, app: FastAPI) -> None:
        async def _employee(_request: Any = None) -> User:
            return User(id=42, label="alice", role="employee")

        app.dependency_overrides[require_api_key] = _employee
        try:
            assert TestClient(app).get("/admin/users/new").status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_post_mints_and_renders_install_command(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._override_admin(app)
        captured: dict[str, Any] = {}

        async def _fake_mint(_pool: Any, *, label: str, role: str) -> tuple[User, str]:
            captured["label"] = label
            captured["role"] = role
            return User(id=99, label=label, role=role), "deadbeef" * 8

        monkeypatch.setattr(admin_ui.users_store, "mint", _fake_mint)
        # _build_install_command reads settings.server_url at call time;
        # patch the module-level reference admin_ui imported.
        monkeypatch.setattr(admin_ui.settings, "server_url", "https://example.test")

        try:
            resp = TestClient(app).post(
                "/admin/users/new",
                data={"label": "  nicholas  ", "role": "employee"},
            )
            assert resp.status_code == 200
            # Label is trimmed before mint
            assert captured == {"label": "nicholas", "role": "employee"}
            # The install command and plaintext both surface in the page
            assert "deadbeef" * 8 in resp.text
            assert "WISPRALT_API_KEY=" in resp.text
            assert "WISPRALT_SERVER=https://example.test" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_post_rejects_empty_label(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._override_admin(app)
        called = {"n": 0}

        async def _fake_mint(*_a: Any, **_kw: Any) -> Any:
            called["n"] += 1
            raise AssertionError("mint must not be called for invalid input")

        monkeypatch.setattr(admin_ui.users_store, "mint", _fake_mint)
        try:
            resp = TestClient(app).post(
                "/admin/users/new", data={"label": "   ", "role": "employee"}
            )
            assert resp.status_code == 400
            assert called["n"] == 0
            assert "Label is required" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_post_rejects_invalid_role(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._override_admin(app)

        async def _fake_mint(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("mint must not be called for invalid role")

        monkeypatch.setattr(admin_ui.users_store, "mint", _fake_mint)
        try:
            resp = TestClient(app).post(
                "/admin/users/new", data={"label": "alex", "role": "superuser"}
            )
            assert resp.status_code == 400
            assert "Invalid role" in resp.text
        finally:
            app.dependency_overrides.clear()

    def test_post_rejects_control_chars_in_label(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._override_admin(app)

        async def _fake_mint(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("mint must not be called for invalid label")

        monkeypatch.setattr(admin_ui.users_store, "mint", _fake_mint)
        try:
            resp = TestClient(app).post(
                "/admin/users/new",
                data={"label": "alex\nadmin", "role": "employee"},
            )
            assert resp.status_code == 400
            assert "control character" in resp.text
        finally:
            app.dependency_overrides.clear()


class TestRequireAdminRoleEnforcement:
    """Direct unit-test of the role gate, independent of HTTP plumbing."""

    def test_admin_user_passes(self) -> None:
        u = User(id=1, label="omid", role="admin")
        assert require_admin(u) is u

    def test_employee_user_raises_403(self) -> None:
        from fastapi import HTTPException

        u = User(id=2, label="alice", role="employee")
        with pytest.raises(HTTPException) as excinfo:
            require_admin(u)
        assert excinfo.value.status_code == 403
