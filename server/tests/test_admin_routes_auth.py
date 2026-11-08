"""
test_admin_routes_auth.py — ``/admin/*`` access control and page data, on a real tmp SQLite DB.

- ``public_router`` (``/admin/login``) is reachable without auth.
- ``authed_router`` is admin-only; ``me_router`` (``/admin/me``) any user.
- The overview, user-detail, usage drilldown and CSV render the numbers
  ``UsageStore`` computes from seeded ``usage_events``.
"""

from __future__ import annotations

import csv
import io
import re
import time
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from wispralt import auth as auth_mod
from wispralt.accounts.service import hash_token
from wispralt.accounts.store import User
from wispralt.auth import SESSION_COOKIE_NAME, require_admin
from wispralt.routes import admin_ui
from wispralt.usage.events import UsageEvent

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore
    from wispralt.jobs.store import JobStore
    from wispralt.usage.store import UsageStore

    from .conftest import MakeUser, Wire


@pytest.fixture
def client(wire: Wire) -> TestClient:
    app = FastAPI()
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.me_router)
    app.include_router(admin_ui.authed_router)
    return TestClient(wire(app), base_url="https://testserver")  # Secure cookies


@pytest.fixture
def admin(make_user: MakeUser) -> tuple[User, dict[str, str]]:
    user, token = make_user(role="admin", label="dana")
    return user, {"Authorization": f"Bearer {token}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── access control ────────────────────────────────────────────────────────────


def test_login_form_is_public(client: TestClient) -> None:
    resp = client.get("/admin/login")
    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]


def test_overview_without_auth_is_401(client: TestClient) -> None:
    assert client.get("/admin/").status_code == 401


def test_overview_employee_is_403(client: TestClient, make_user: MakeUser) -> None:
    _u, token = make_user()
    assert client.get("/admin/", headers=_bearer(token)).status_code == 403


def test_overview_admin_is_200(client: TestClient, admin: tuple[User, dict[str, str]]) -> None:
    resp = client.get("/admin/", headers=admin[1])
    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]


def test_login_sets_cookie_and_cookie_authenticates(
    client: TestClient, make_user: MakeUser
) -> None:
    _admin, token = make_user(role="admin")
    form = client.get("/admin/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    resp = client.post(
        "/admin/login", data={"token": token, "csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/"
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/admin/").status_code == 200


def test_login_rejects_unknown_token(client: TestClient) -> None:
    form = client.get("/admin/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    resp = client.post("/admin/login", data={"token": "f" * 64, "csrf_token": csrf})
    assert resp.status_code == 401


def test_admin_me_redirects_admin_and_renders_employee(
    client: TestClient, make_user: MakeUser, admin: tuple[User, dict[str, str]]
) -> None:
    resp = client.get("/admin/me", headers=admin[1], follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/"
    emp, token = make_user(label="alice")
    resp = client.get("/admin/me", headers=_bearer(token))
    assert resp.status_code == 200 and "alice" in resp.text


def test_require_admin_role_gate() -> None:
    admin_user = User(id=1, key_id=None, label="dana", role="admin", kind="employee")
    assert require_admin(admin_user) is admin_user
    with pytest.raises(HTTPException) as excinfo:
        require_admin(User(id=2, key_id=None, label="alice", role="employee", kind="employee"))
    assert excinfo.value.status_code == 403


# ── employees ─────────────────────────────────────────────────────────────────


def test_add_employee_form_admin_only(
    client: TestClient, make_user: MakeUser, admin: tuple[User, dict[str, str]]
) -> None:
    assert "Add employee" in client.get("/admin/people/new", headers=admin[1]).text
    _u, token = make_user()
    assert client.get("/admin/people/new", headers=_bearer(token)).status_code == 403


def test_add_employee_creates_user_and_install_command(
    client: TestClient,
    admin: tuple[User, dict[str, str]],
    accounts: AccountStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_ui.settings, "server_url", "https://example.test")
    resp = client.post(
        "/admin/people/new", data={"label": "  sam  ", "role": "employee"}, headers=admin[1]
    )
    assert resp.status_code == 200, resp.text
    assert "./install.sh --role client --client-mode local" in resp.text
    assert "https://example.test" in resp.text and "curl" not in resp.text
    [token] = set(re.findall(r"\b[0-9a-f]{64}\b", resp.text))
    user = accounts.lookup_key(hash_token(token))
    assert user is not None and user.label == "sam" and user.kind == "employee"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"label": "   ", "role": "employee"}, "Label is required"),
        ({"label": "alex", "role": "superuser"}, "Invalid role"),
        ({"label": "alex\nadmin", "role": "employee"}, "control character"),
    ],
)
def test_add_employee_rejects_invalid_input(
    client: TestClient,
    admin: tuple[User, dict[str, str]],
    accounts: AccountStore,
    data: dict[str, str],
    message: str,
) -> None:
    resp = client.post("/admin/people/new", data=data, headers=admin[1])
    assert resp.status_code == 400 and message in resp.text
    assert [u.label for u in accounts.list_people()] == ["dana"]


def test_users_page_shows_disabled_state(
    client: TestClient, make_user: MakeUser, admin: tuple[User, dict[str, str]]
) -> None:
    emp, token = make_user(label="bob")
    resp = client.post(f"/admin/people/{emp.id}/disable", headers=admin[1], follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/admin/", headers=_bearer(token)).status_code == 401
    page = client.get("/admin/people", headers=admin[1]).text
    assert "bob" in page and "disabled" in page


# ── page numbers come from UsageStore ─────────────────────────────────────────


@pytest.fixture
def seeded(make_user: MakeUser, usage: UsageStore) -> User:
    alice, _ = make_user(label="alice")
    now = time.time()
    for ts, status, ms in [
        (now - 60, 200, 100.0),
        (now - 120, 200, 300.0),
        (now - 3600, 500, 200.0),
        (now - 3 * 86400, 200, 50.0),
        (now - 20 * 86400, 200, 50.0),
    ]:
        usage.record(UsageEvent(user_id=alice.id, ts=ts, kind="dictate", status=status, duration_ms=ms))
    return alice


def _tiles(html: str) -> dict[str, str]:
    labels = re.findall(r'<div class="label">([^<]+)</div>\s*<div class="value">([^<]*)</div>', html)
    return {k.strip(): v.strip() for k, v in labels}


def test_overview_numbers_match_the_stores(
    client: TestClient,
    admin: tuple[User, dict[str, str]],
    seeded: User,
    job_store: JobStore,
) -> None:
    now = time.time()
    for age in (60, 3 * 86400):
        job_store.insert_dictation(
            user_id=seeded.id,
            text="hello",
            duration_ms=None,
            client_app_version=None,
            dedup_id=f"00000000-0000-4000-8000-{age:012d}",
            created_at=now - age,
        )
    html = client.get("/admin/", headers=admin[1]).text
    values = _tiles(html)
    # Dictation tiles count dictation rows (job_store), not usage_events requests.
    assert (values["Dictations 24h"], values["Dictations 7d"], values["Dictations 30d"]) == (
        "1",
        "2",
        "2",
    )
    assert values["Errors 24h"] == "1"  # usage_events telemetry
    assert values["p50 latency 24h"] == "200 ms"
    assert "alice" in html


def test_streamed_dictation_counts_once(
    client: TestClient,
    admin: tuple[User, dict[str, str]],
    make_user: MakeUser,
    usage: UsageStore,
    job_store: JobStore,
) -> None:
    bob, _ = make_user(label="bob")
    now = time.time()
    for _chunk_or_finalize in range(4):  # 3 stream chunks + 1 finalize
        usage.record(UsageEvent(user_id=bob.id, ts=now, kind="dictate", status=202))
    job_store.insert_dictation(
        user_id=bob.id, text="one dictation", duration_ms=100, client_app_version=None,
        dedup_id=None,
    )
    values = _tiles(client.get("/admin/", headers=admin[1]).text)
    assert values["Dictations 24h"] == "1"


def test_user_detail_and_usage_pages(
    client: TestClient, admin: tuple[User, dict[str, str]], seeded: User
) -> None:
    detail = client.get(f"/admin/people/{seeded.id}", headers=admin[1])
    assert detail.status_code == 200 and "alice" in detail.text
    assert client.get("/admin/people/9999", headers=admin[1]).status_code == 404

    page = client.get("/admin/usage", params={"status": 500}, headers=admin[1])
    assert page.status_code == 200 and page.text.count("<td>500</td>") == 1

    resp = client.get("/admin/usage.csv", params={"user_id": seeded.id}, headers=admin[1])
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 5 and {r["user_label"] for r in rows} == {"alice"}


# ── CSRF on every cookie-authenticated mutation (AUDIT #6/#7) ─────────────────

_CSRF = "csrf-test-token"


@pytest.fixture
def csrf_client(wire: Wire) -> TestClient:
    from wispralt.routes.me import router as me_router

    app = FastAPI()
    app.include_router(admin_ui.public_router)
    app.include_router(admin_ui.me_router)
    app.include_router(admin_ui.authed_router)
    app.include_router(me_router)
    return TestClient(wire(app), base_url="https://testserver")


def _mutation(name: str, employee: User, integration: User) -> tuple[str, str, dict, int]:
    """(method, path, body kwargs, success status) for each guarded mutation."""
    return {
        "people_new": ("POST", "/admin/people/new", {"data": {"label": "newbie"}}, 200),
        "people_disable": ("POST", f"/admin/people/{employee.id}/disable", {"data": {}}, 303),
        "people_rotate": ("POST", f"/admin/people/{employee.id}/rotate", {"data": {}}, 200),
        "device_revoke": ("POST", f"/admin/keys/{employee.key_id}/revoke", {"data": {}}, 303),
        "integrations_new": ("POST", "/admin/integrations/new", {"data": {"program_name": "Buzz"}}, 200),
        "integrations_revoke": ("POST", f"/admin/integrations/{integration.id}/revoke", {"data": {}}, 303),
        "integrations_rotate": ("POST", f"/admin/integrations/{integration.id}/rotate", {"data": {}}, 200),
        "patch_me": ("PATCH", "/me", {"json": {"display_name": "Dana Z"}}, 200),
    }[name]


@pytest.mark.parametrize(
    "name",
    [
        "people_new",
        "people_disable",
        "people_rotate",
        "device_revoke",
        "integrations_new",
        "integrations_revoke",
        "integrations_rotate",
        "patch_me",
    ],
)
@pytest.mark.parametrize(
    "mode", ["cookie_no_token", "cookie_token", "bearer_only", "basic_plus_cookie", "bad_bearer_cookie"]
)
def test_csrf_matrix(
    csrf_client: TestClient,
    admin: tuple[User, dict[str, str]],
    make_user: MakeUser,
    name: str,
    mode: str,
) -> None:
    admin_user, bearer = admin
    token = bearer["Authorization"].removeprefix("Bearer ")
    employee, _ = make_user(label="emp")
    integration, _ = make_user(kind="integration", label="key-buzz")
    method, path, body, ok = _mutation(name, employee, integration)

    headers: dict[str, str] = {}
    if mode == "bearer_only":
        headers = dict(bearer)
    else:
        csrf_client.cookies.set(SESSION_COOKIE_NAME, token)
    if mode == "basic_plus_cookie":
        headers = {"Authorization": "Basic eDp5"}
    if mode == "bad_bearer_cookie":
        headers = {"Authorization": "Bearer " + "0" * 64}
    if mode == "cookie_token":
        csrf_client.cookies.set(auth_mod.CSRF_COOKIE_NAME, _CSRF)
        if "json" in body:
            headers[auth_mod.CSRF_HEADER] = _CSRF
        else:
            body = {"data": {**body["data"], "csrf_token": _CSRF}}

    resp = csrf_client.request(method, path, headers=headers, follow_redirects=False, **body)

    expected = {
        "cookie_no_token": 403,
        "cookie_token": ok,
        "bearer_only": ok,
        "basic_plus_cookie": 403,
        "bad_bearer_cookie": 401,
    }[mode]
    assert resp.status_code == expected, resp.text


def test_form_pages_embed_the_csrf_token_and_round_trip(
    csrf_client: TestClient, admin: tuple[User, dict[str, str]], make_user: MakeUser
) -> None:
    make_user(kind="integration", label="key-buzz")  # so /admin/integrations renders its forms
    token = admin[1]["Authorization"].removeprefix("Bearer ")
    csrf_client.cookies.set(SESSION_COOKIE_NAME, token)
    for page in (
        f"/admin/people/{admin[0].id}",
        "/admin/integrations",
        "/admin/people/new",
        "/admin/integrations/new",
    ):
        resp = csrf_client.get(page)
        assert resp.status_code == 200, page
        assert auth_mod.CSRF_COOKIE_NAME in resp.headers.get("set-cookie", ""), page
        assert re.search(r'name="csrf_token" value="[^"]+"', resp.text), page
    form = csrf_client.get("/admin/integrations/new")  # the browser keeps the cookie it just got
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    resp = csrf_client.post("/admin/integrations/new", data={"program_name": "Open WebUI", "csrf_token": csrf})
    assert resp.status_code == 200, resp.text


def test_key_added_shows_the_configured_server_url(
    csrf_client: TestClient, admin: tuple[User, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin_ui.settings, "server_url", "https://mini.example.test/")
    resp = csrf_client.post("/admin/integrations/new", data={"program_name": "Buzz"}, headers=admin[1])
    assert "export OPENAI_BASE_URL=https://mini.example.test/v1" in resp.text
