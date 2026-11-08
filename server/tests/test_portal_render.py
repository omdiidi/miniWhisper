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
    return make_user(role="admin", label="omid"), make_user(label="alice")


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
