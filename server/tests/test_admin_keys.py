"""
test_admin_keys.py — /admin/integrations/* (integration API keys) on a real tmp SQLite DB.

Auth is real: requests carry a Bearer token minted by ``make_user`` on the
per-test DB, and the routes write through ``AccountStore``.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from wispralt.accounts.service import hash_token
from wispralt.routes import admin_ui

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore

    from .conftest import MakeUser, Wire


@pytest.fixture
def client(wire: Wire) -> TestClient:
    app = FastAPI()
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.authed_router)
    return TestClient(wire(app))


@pytest.fixture
def admin_headers(make_user: MakeUser) -> dict[str, str]:
    _admin, token = make_user(role="admin", label="dana")
    return {"Authorization": f"Bearer {token}"}


def test_get_keys_requires_admin(client: TestClient, make_user: MakeUser) -> None:
    _emp, token = make_user()
    resp = client.get("/admin/integrations", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_get_new_form_renders(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.get("/admin/integrations/new", headers=admin_headers)
    assert resp.status_code == 200
    assert "Add API key" in resp.text


def test_post_new_creates_integration_user_and_shows_token_once(
    client: TestClient, admin_headers: dict[str, str], accounts: AccountStore
) -> None:
    resp = client.post("/admin/integrations/new", data={"program_name": "Open WebUI"}, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert "OPENAI_BASE_URL=" in resp.text and "OPENAI_API_KEY=" in resp.text
    [row] = accounts.list_integrations()
    assert row.label == "key-open-webui" and row.display_name == "Open WebUI"
    assert row.kind == "integration" and row.role == "employee"
    # The page shows the one plaintext that authenticates as that user.
    [token] = set(re.findall(r"\b[0-9a-f]{64}\b", resp.text))
    user = accounts.lookup_key(hash_token(token))
    assert user is not None and user.id == row.id and user.kind == "integration"


def test_post_new_invalid_program_name(
    client: TestClient, admin_headers: dict[str, str], accounts: AccountStore
) -> None:
    resp = client.post("/admin/integrations/new", data={"program_name": "   "}, headers=admin_headers)
    assert resp.status_code == 400, resp.text
    assert accounts.list_integrations() == []


def test_revoke_disables_the_key_for_the_next_request(
    client: TestClient,
    admin_headers: dict[str, str],
    make_user: MakeUser,
    accounts: AccountStore,
) -> None:
    key_user, token = make_user(kind="integration", label="key-buzz")
    assert accounts.lookup_key(hash_token(token)) is not None
    resp = client.post(f"/admin/integrations/{key_user.id}/revoke", headers=admin_headers, follow_redirects=False)
    assert resp.status_code == 303
    assert accounts.lookup_key(hash_token(token)) is None
    assert accounts.list_integrations() == []


def test_rotate_replaces_the_key(
    client: TestClient,
    admin_headers: dict[str, str],
    make_user: MakeUser,
    accounts: AccountStore,
) -> None:
    key_user, old = make_user(kind="integration", label="key-buzz")
    accounts.update_display_name(key_user.id, "Buzz")
    resp = client.post(f"/admin/integrations/{key_user.id}/rotate", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert "rotated" in resp.text.lower() and "Buzz" in resp.text
    assert accounts.lookup_key(hash_token(old)) is None
    assert old not in resp.text


def test_rotate_unknown_user_is_404(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.post("/admin/integrations/9999/rotate", headers=admin_headers).status_code == 404


def test_overview_shows_integration_count(
    client: TestClient, admin_headers: dict[str, str], make_user: MakeUser
) -> None:
    for _ in range(3):
        make_user(kind="integration")
    resp = client.get("/admin/", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert "Integration keys" in resp.text
    assert re.search(r"Integration keys</a></div>\s*<div class=\"value\">3</div>", resp.text)
