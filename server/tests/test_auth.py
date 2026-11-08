"""
test_auth.py — bearer/cookie auth against the local SQLite DB (no cache, no fallback admin).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

import pytest
from fastapi import Depends, FastAPI, Request
from starlette.testclient import TestClient

from wispralt.accounts.store import AccountStore, User
from wispralt.auth import SESSION_COOKIE_NAME, require_api_key, require_api_key_v1
from wispralt.database import Database
from wispralt.routes import admin_ui

if TYPE_CHECKING:
    from .conftest import MakeUser, Wire


@pytest.fixture
def client(wire: Wire) -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    async def _whoami(request: Request, user: User = Depends(require_api_key)) -> dict:
        assert request.state.user == user
        return {"id": user.id, "role": user.role, "via": request.state.auth_via}

    @app.get("/v1/whoami")
    async def _v1(user: User = Depends(require_api_key_v1)) -> dict:
        return {"id": user.id}

    app.include_router(admin_ui.authed_router)
    return TestClient(wire(app))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_key_authenticates(client: TestClient, make_user: MakeUser) -> None:
    user, token = make_user()
    resp = client.get("/whoami", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json() == {"id": user.id, "role": "employee", "via": "bearer"}


def test_cookie_authenticates_but_not_on_v1(client: TestClient, make_user: MakeUser) -> None:
    user, token = make_user()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/whoami").json() == {"id": user.id, "role": "employee", "via": "cookie"}
    assert client.get("/v1/whoami").status_code == 401


def test_unknown_missing_revoked_and_disabled_are_401(
    client: TestClient, make_user: MakeUser, accounts: AccountStore
) -> None:
    assert client.get("/whoami").status_code == 401
    assert client.get("/whoami", headers=_bearer("f" * 64)).status_code == 401

    revoked, token = make_user()
    accounts.revoke_key(revoked.key_id)
    assert client.get("/whoami", headers=_bearer(token)).status_code == 401

    disabled, token2 = make_user()
    accounts.disable_user(disabled.id)
    assert client.get("/whoami", headers=_bearer(token2)).status_code == 401


def test_db_error_is_503_with_no_fallback(
    client: TestClient,
    make_user: MakeUser,
    accounts: AccountStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _admin, token = make_user(role="admin")

    def _boom(_hash: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(accounts, "lookup_key", _boom)
    assert client.get("/whoami", headers=_bearer(token)).status_code == 503
    assert client.get("/admin/", headers=_bearer(token)).status_code == 503


def test_revoke_from_another_handle_applies_to_next_request(
    client: TestClient, make_user: MakeUser, db: Database
) -> None:
    user, token = make_user()
    assert client.get("/whoami", headers=_bearer(token)).status_code == 200
    # A second Database on the same file stands in for another process (the Part 5 CLI).
    AccountStore(Database(db.path)).revoke_key(user.key_id)
    assert client.get("/whoami", headers=_bearer(token)).status_code == 401


def test_disable_then_mint_gives_a_working_key(
    client: TestClient,
    make_user: MakeUser,
    accounts: AccountStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _admin, admin_token = make_user(role="admin")
    emp, old = make_user()
    accounts.disable_user(emp.id)
    with caplog.at_level(logging.DEBUG):
        resp = client.post(f"/admin/users/{emp.id}/mint", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    [new] = set(re.findall(r"\b[0-9a-f]{64}\b", resp.text))
    assert client.get("/whoami", headers=_bearer(new)).json()["id"] == emp.id
    assert client.get("/whoami", headers=_bearer(old)).status_code == 401
    assert not any(new in r.getMessage() for r in caplog.records)
