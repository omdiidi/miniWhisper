"""Part 6 Task 2: every portal page renders in the new shell, role-aware nav, no inline CSS.

Uses the real ``create_app()`` route table (no lifespan: ``wire`` puts the per-test DB
on ``app.state``), so a page that is not mounted fails here too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from wispralt.main import create_app

if TYPE_CHECKING:
    from wispralt.accounts.store import User

    from .conftest import MakeUser, Wire

_TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "wispralt" / "admin" / "templates"

ADMIN_PAGES = [
    "/admin/",
    "/admin/people",
    "/admin/people/new",
    "/admin/people/{uid}",
    "/admin/integrations",
    "/admin/integrations/new",
    "/admin/invites",
    "/admin/usage",
    "/admin/records",
    "/admin/data",
    "/admin/system",
    "/admin/settings",
]
SELF_PAGES = ["/me/history", "/me/insights"]
ADMIN_NAV_LINKS = (
    'href="/admin/people"',
    'href="/admin/integrations"',
    'href="/admin/invites"',
    'href="/admin/data"',
    'href="/admin/usage"',
    'href="/admin/system"',
    'href="/admin/records"',
    'href="/admin/settings"',
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from wispralt.ops import monitoring

    async def fake_probe(url: str) -> bool:
        return False

    monkeypatch.setattr(monitoring, "probe_reachable", fake_probe)


@pytest.fixture
def client(wire: Wire) -> TestClient:
    return TestClient(wire(create_app()), base_url="https://testserver")


@pytest.fixture
def people(make_user: MakeUser) -> tuple[tuple[User, str], tuple[User, str]]:
    return make_user(role="admin", label="dana"), make_user(label="alice")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("page", ADMIN_PAGES + SELF_PAGES)
def test_admin_sees_every_page_in_the_shell(client: TestClient, people, page: str) -> None:
    (admin, token), (alice, _) = people
    resp = client.get(page.format(uid=alice.id), headers=_auth(token))
    assert resp.status_code == 200, (page, resp.text[:300])
    html = resp.text
    assert 'href="/admin/static/portal.css"' in html
    assert 'class="sidebar"' in html and 'aria-current="page"' in html
    for link in ADMIN_NAV_LINKS:
        assert link in html


@pytest.mark.parametrize("page", ADMIN_PAGES)
def test_employee_is_forbidden_on_admin_pages(client: TestClient, people, page: str) -> None:
    (_admin, _), (alice, token) = people
    assert client.get(page.format(uid=alice.id), headers=_auth(token)).status_code == 403


@pytest.mark.parametrize("page", [*SELF_PAGES, "/admin/me"])
def test_employee_self_pages_have_no_admin_links(client: TestClient, people, page: str) -> None:
    _admin, (_alice, token) = people
    resp = client.get(page, headers=_auth(token))
    assert resp.status_code == 200, resp.text[:300]
    assert 'href="/me/history"' in resp.text and 'href="/me/insights"' in resp.text
    for link in ADMIN_NAV_LINKS:
        assert link not in resp.text


def test_login_pages_render_without_chrome(client: TestClient) -> None:
    for page in ("/admin/login", "/me/login"):
        resp = client.get(page)
        assert resp.status_code == 200
        assert 'class="sidebar"' not in resp.text and "portal.css" in resp.text


def test_stylesheet_is_served(client: TestClient) -> None:
    resp = client.get("/admin/static/portal.css")
    assert resp.status_code == 200 and "prefers-color-scheme: dark" in resp.text
    assert "max-width: 768px" in resp.text


def test_no_inline_style_blocks_in_templates() -> None:
    offenders = [p.name for p in _TEMPLATES.glob("*.j2") if re.search(r"<style\b", p.read_text())]
    assert offenders == []


# ── XSS regressions: request/user data never reaches a JS/Alpine expression ──────

_HOSTILE_RANGE = "7d' + alert(document.domain) + '"
# Attributes whose value the browser (inline handlers) or Alpine/HTMX evaluates as code.
_JS_ATTR = re.compile(
    r"""(?<![\w-])(on[a-z]+|hx-on[\w:.-]*|x-[\w:.-]+|@[\w:.-]+|:[\w.-]+)\s*=\s*("[^"]*"|'[^']*')""",
)


@pytest.mark.parametrize(
    ("page", "default"),
    [("/me/insights", "7d"), ("/me/history", "30d"), ("/admin/data", "7d"), ("/admin/records", "30d")],
)
def test_hostile_range_is_rejected_and_never_rendered(
    client: TestClient, people, page: str, default: str,
) -> None:
    (_admin, token), _ = people
    resp = client.get(page, params={"range": _HOSTILE_RANGE}, headers=_auth(token))
    assert resp.status_code == 200, resp.text[:300]
    assert "alert(" not in resp.text
    if 'class="tabs"' in resp.text:
        assert f'data-active="{default}"' in resp.text


def test_hostile_label_stays_out_of_confirm_handlers(client: TestClient, make_user) -> None:
    _admin, token = make_user(role="admin", label="root")
    evil, _ = make_user(label="x'); alert(1); ('")
    resp = client.get(f"/admin/people/{evil.id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text[:300]
    handlers = [m.group(2) for m in _JS_ATTR.finditer(resp.text)]
    assert handlers and not any("alert" in h for h in handlers)
    assert "data-confirm=\"Disable x&#39;); alert(1); (&#39;?" in resp.text


def test_no_template_interpolates_into_a_js_attribute() -> None:
    offenders = [
        f"{p.name}: {m.group(0)[:80]}"
        for p in _TEMPLATES.glob("*.j2")
        for m in _JS_ATTR.finditer(p.read_text())
        if "{{" in m.group(2) or "{%" in m.group(2)
    ]
    assert offenders == []


def test_settings_page_points_at_the_real_env_file_when_the_key_is_missing(
    client: TestClient, people, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wispralt.config import settings

    monkeypatch.setattr(settings, "openrouter_api_key", None)
    (_admin, token), _ = people
    html = client.get("/admin/settings", headers=_auth(token)).text
    assert "~/.config/wispralt/.env" in html and "wispralt restart" in html
    assert "server/.env" not in html and "the CLI does this" not in html


# ── Secure cookies vs plain-http loopback (setup opens http://127.0.0.1:8000/admin) ──


def _cookie_headers(resp, name: str) -> list[str]:  # noqa: ANN001
    """The ``name`` cookies being SET (not the legacy-path deletions, which expire them)."""
    return [h for h in resp.headers.get_list("set-cookie")
            if h.startswith(f"{name}=") and "max-age=0" not in h.lower()]


@pytest.mark.parametrize(
    ("base_url", "secure"),
    [
        ("http://127.0.0.1:8000", False),
        ("http://localhost:8000", False),
        ("https://127.0.0.1:8000", True),
        ("http://wispralt.example.com", True),
        ("https://wispralt.example.com", True),
    ],
)
def test_portal_cookies_drop_secure_only_on_plain_http_loopback(
    wire: Wire, make_user, base_url: str, secure: bool,
) -> None:
    from wispralt.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

    _admin, token = make_user(role="admin", label="root")
    c = TestClient(wire(create_app()), base_url=base_url)
    form = c.get("/admin/login")
    [csrf_cookie] = _cookie_headers(form, CSRF_COOKIE_NAME)
    assert ("secure" in csrf_cookie.lower()) is secure, csrf_cookie
    if base_url.startswith("http://") and secure:
        return  # a Secure cookie is (correctly) not sent back over plain http
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
    resp = c.post("/admin/login", data={"token": token, "csrf_token": csrf},
                  follow_redirects=False)
    assert resp.status_code == 303, resp.text[:300]
    [session_cookie] = _cookie_headers(resp, SESSION_COOKIE_NAME)
    assert ("secure" in session_cookie.lower()) is secure, session_cookie
    assert "httponly" in session_cookie.lower() and "samesite=strict" in session_cookie.lower()


@pytest.mark.parametrize(
    ("scheme", "host", "secure"),
    [("http", "[::1]:8000", False), ("http", "127.0.0.2", False), ("https", "[::1]", True),
     ("http", "10.0.0.5:8000", True), ("http", "localhost.evil.example", True)],
)
def test_cookie_secure_by_scheme_and_host(scheme: str, host: str, secure: bool) -> None:
    from starlette.requests import Request

    from wispralt.auth import cookie_secure

    req = Request({"type": "http", "scheme": scheme, "path": "/", "query_string": b"",
                   "headers": [(b"host", host.encode())]})
    assert cookie_secure(req) is secure
    assert cookie_secure(None) is True
