"""Part 6 Task 4: people, devices and keys in the portal — rotate vs revoke vs disable, CSRF, roles."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from wispralt.accounts import service
from wispralt.accounts.service import hash_token
from wispralt.auth import SESSION_COOKIE_NAME
from wispralt.main import create_app
from wispralt.usage.events import UsageEvent

if TYPE_CHECKING:
    from fastapi import FastAPI

    from wispralt.accounts.store import AccountStore, User
    from wispralt.usage.store import UsageStore

    from .conftest import MakeUser, Wire


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    return wire(create_app())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin(make_user: MakeUser) -> tuple[User, str]:
    return make_user(role="admin", label="dana")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _second_device(accounts: AccountStore, admin: User, person: User) -> str:
    _iid, invite, _ = service.create_invite(accounts, created_by=admin.id, user_id=person.id)
    _user, token = service.redeem_invite(accounts, invite, "laptop")
    return token


def test_rotate_leaves_exactly_one_active_key_and_reenables(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    alice, old = make_user(label="alice")
    other = _second_device(accounts, admin[0], alice)
    accounts.disable_user(alice.id)
    resp = client.post(f"/admin/people/{alice.id}/rotate", headers=_bearer(admin[1]))
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    [new] = set(re.findall(r"\b[0-9a-f]{64}\b", resp.text))
    assert len([k for k in accounts.list_keys(alice.id) if k.revoked_at is None]) == 1
    assert client.get("/me", headers=_bearer(old)).status_code == 401
    assert client.get("/me", headers=_bearer(other)).status_code == 401
    assert client.get("/me", headers=_bearer(new)).status_code == 200  # re-enabled


def test_revoking_one_device_leaves_the_other_working(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    alice, first = make_user(label="alice")
    second = _second_device(accounts, admin[0], alice)
    resp = client.post(
        f"/admin/keys/{alice.key_id}/revoke", headers=_bearer(admin[1]), follow_redirects=False
    )
    assert resp.status_code == 303 and resp.headers["location"] == f"/admin/people/{alice.id}"
    assert client.get("/me", headers=_bearer(first)).status_code == 401
    assert client.get("/me", headers=_bearer(second)).status_code == 200
    assert client.post("/admin/keys/9999/revoke", headers=_bearer(admin[1])).status_code == 404


def test_disable_person_blocks_every_key_and_not_yourself(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    alice, first = make_user(label="alice")
    second = _second_device(accounts, admin[0], alice)
    resp = client.post(
        f"/admin/people/{alice.id}/disable", headers=_bearer(admin[1]), follow_redirects=False
    )
    assert resp.status_code == 303
    for token in (first, second):
        assert client.get("/me", headers=_bearer(token)).status_code == 401
    assert client.post(f"/admin/people/{admin[0].id}/disable", headers=_bearer(admin[1])).status_code == 400


def test_person_page_lists_devices_with_last_seen(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore, usage: UsageStore
) -> None:
    alice, _ = make_user(label="alice")
    _second_device(accounts, admin[0], alice)
    usage.record(UsageEvent(user_id=alice.id, ts=time.time() - 30, kind="dictate", status=200,
                            key_id=alice.key_id))
    keys = {k.id: k for k in accounts.list_keys(alice.id)}
    assert keys[alice.key_id].last_seen_at is not None
    assert [k.last_seen_at for k in keys.values() if k.id != alice.key_id] == [None]
    page = client.get(f"/admin/people/{alice.id}", headers=_bearer(admin[1]))
    assert page.status_code == 200
    assert "laptop" in page.text and "Revoke device" in page.text and "Add device" in page.text


def test_a_tracked_request_records_its_key(
    client: TestClient, make_user: MakeUser, usage: UsageStore, db
) -> None:
    alice, token = make_user()
    client.post("/transcribe/dictate", headers=_bearer(token))  # tracked: a 4xx still records
    with db.read() as con:
        rows = con.execute("SELECT key_id FROM usage_events WHERE user_id = ?", (alice.id,)).fetchall()
    assert rows and all(r[0] == alice.key_id for r in rows)


def _new_posts(person: User, integration: User, invite_id: int) -> list[tuple[str, dict]]:
    return [
        (f"/admin/people/{person.id}/rotate", {}),
        (f"/admin/people/{person.id}/disable", {}),
        (f"/admin/keys/{person.key_id}/revoke", {}),
        ("/admin/invites", {"label": "newbie"}),
        ("/admin/invites", {"user_id": person.id}),
        (f"/admin/invites/{invite_id}/revoke", {}),
        ("/admin/people/new", {"label": "x"}),
        ("/admin/integrations/new", {"program_name": "Buzz"}),
        (f"/admin/integrations/{integration.id}/rotate", {}),
        (f"/admin/integrations/{integration.id}/revoke", {}),
    ]


@pytest.mark.parametrize("index", range(10))
def test_new_posts_need_csrf_by_cookie_work_by_bearer_and_refuse_employees(
    app: FastAPI, admin, make_user: MakeUser, accounts: AccountStore, index: int
) -> None:
    person, _ = make_user(label="alice")
    integration, _ = make_user(kind="integration", label="key-buzz")
    _emp, emp_token = make_user(label="emp")
    invite_id, _tok, _ = service.create_invite(accounts, created_by=admin[0].id, label="p")
    path, data = _new_posts(person, integration, invite_id)[index]

    cookie_client = TestClient(app, base_url="https://testserver")
    cookie_client.cookies.set(SESSION_COOKIE_NAME, admin[1])
    assert cookie_client.post(path, data=data, follow_redirects=False).status_code == 403

    employee = TestClient(app, base_url="https://testserver")
    assert employee.post(path, data=data, headers=_bearer(emp_token)).status_code == 403

    resp = TestClient(app, base_url="https://testserver").post(
        path, data=data, headers=_bearer(admin[1]), follow_redirects=False
    )
    assert resp.status_code in (200, 303), (path, resp.text[:200])
    assert accounts.lookup_key(hash_token(admin[1])) is not None
