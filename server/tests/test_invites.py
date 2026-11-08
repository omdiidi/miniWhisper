"""Part 6 Task 4: invites — create (shown once), atomic single-use redeem, uniform errors, rate limit."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from wispralt.accounts import service
from wispralt.accounts.service import hash_token
from wispralt.main import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI

    from wispralt.accounts.store import AccountStore, User
    from wispralt.database import Database

    from .conftest import MakeUser, Wire

_INVALID = {"error": "invite_invalid"}


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    return wire(create_app())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin(make_user: MakeUser) -> tuple[User, dict[str, str]]:
    user, token = make_user(role="admin", label="omid")
    return user, {"Authorization": f"Bearer {token}"}


def _create(client: TestClient, headers: dict[str, str], **data: object) -> tuple[str, str]:
    resp = client.post("/admin/invites", data=data, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    link = re.search(r"wispralt://join\?[^<\s]+", resp.text).group(0)
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(link.replace("&amp;", "&")).query)["invite"][0]
    return link, token


def _redeem(client: TestClient, invite: str, ip: str = "203.0.113.1", **extra: object):
    return client.post(
        "/invites/redeem", json={"invite": invite, **extra}, headers={"CF-Connecting-IP": ip}
    )


def test_create_shows_link_once_and_stores_only_the_hash(
    client: TestClient, admin, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wispralt.routes import admin_invites

    monkeypatch.setattr(admin_invites.settings, "server_url", "https://mini.example.test")
    link, token = _create(client, admin[1], label="sarah", role="employee")
    assert link.startswith("wispralt://join?host=https%3A%2F%2Fmini.example.test&amp;invite=")
    assert token in link
    with db.read() as con:
        [row] = con.execute("SELECT token_hash, expires_at - created_at FROM invites").fetchall()
        dump = "\n".join(con.iterdump())
    assert row[0] == hash_token(token) and row[1] == 600
    assert token not in dump


def test_redeem_issues_a_key_that_authenticates_next_request(
    client: TestClient, admin, accounts: AccountStore, caplog: pytest.LogCaptureFixture
) -> None:
    _link, invite = _create(client, admin[1], label="sarah", role="employee")
    with caplog.at_level(logging.DEBUG):
        resp = _redeem(client, invite, device_label="Sarah's MacBook")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_label"] == "sarah" and body["host"]
    assert resp.headers["cache-control"] == "no-store"
    me = client.get("/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["label"] == "sarah"
    user = accounts.lookup_key(hash_token(body["token"]))
    [key] = accounts.list_keys_for_user(user.id)
    assert key.label == "Sarah's MacBook"
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert invite not in messages and body["token"] not in messages


def test_every_failure_is_the_same_400(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    admin_user, headers = admin
    _l, used = _create(client, headers, label="a", role="employee")
    assert _redeem(client, used).status_code == 200
    _iid, expired, _exp = service.create_invite(
        accounts, created_by=admin_user.id, label="b", now=time.time() - 601
    )
    other_admin, _ = make_user(role="admin")
    _iid, orphaned, _exp = service.create_invite(accounts, created_by=other_admin.id, label="c")
    accounts.disable_user(other_admin.id)
    _l, revoked = _create(client, headers, label="d", role="employee")
    [pending] = [i for i in accounts.list_invites(time.time()) if i.label == "d"]
    assert client.post(f"/admin/invites/{pending.id}/revoke", headers=headers,
                       follow_redirects=False).status_code == 303

    bodies = []
    for token in (used, expired, orphaned, revoked, "no-such-invite"):
        resp = _redeem(client, token)
        bodies.append((resp.status_code, resp.content))
    for bad in (b"not json", b"[]", b'{"invite": 5}', b"{}"):
        resp = client.post("/invites/redeem", content=bad, headers={"CF-Connecting-IP": "198.51.100.9"})
        bodies.append((resp.status_code, resp.content))
    assert set(bodies) == {(400, b'{"error":"invite_invalid"}')}
    assert {u.label for u in accounts.list_people()} == {"omid", "a", "user-1"}


def test_eleventh_attempt_in_a_minute_is_429(client: TestClient) -> None:
    codes = [_redeem(client, f"x{i}", ip="192.0.2.7").status_code for i in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429
    assert _redeem(client, "x", ip="192.0.2.8").status_code == 400  # per client IP


def test_add_device_keeps_the_old_key_working(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    alice, old = make_user(label="alice")
    _l, invite = _create(client, admin[1], user_id=alice.id)
    resp = _redeem(client, invite, device_label="iMac")
    assert resp.status_code == 200 and resp.json()["user_label"] == "alice"
    new = resp.json()["token"]
    assert accounts.lookup_key(hash_token(old)).id == alice.id
    assert accounts.lookup_key(hash_token(new)).id == alice.id
    assert len(accounts.list_keys_for_user(alice.id)) == 2


def test_add_device_for_unknown_or_disabled_person_is_404(
    client: TestClient, admin, make_user: MakeUser, accounts: AccountStore
) -> None:
    bob, _ = make_user(label="bob")
    accounts.disable_user(bob.id)
    for uid in (bob.id, 9999):
        assert client.post("/admin/invites", data={"user_id": uid}, headers=admin[1]).status_code == 404


def test_invalid_new_person_label_is_400(client: TestClient, admin) -> None:
    assert client.post("/admin/invites", data={"label": "  "}, headers=admin[1]).status_code == 400
    resp = client.post("/admin/invites", data={"label": "x", "role": "root"}, headers=admin[1])
    assert resp.status_code == 400


async def test_concurrent_redeems_of_one_invite_issue_one_key(
    app: FastAPI, admin, accounts: AccountStore
) -> None:
    _iid, invite, _exp = service.create_invite(accounts, created_by=admin[0].id, label="z")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
        results = await asyncio.gather(
            *(ac.post("/invites/redeem", json={"invite": invite}) for _ in range(4))
        )
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400]
    assert [u.label for u in accounts.list_people() if u.label == "z"] == ["z"]
