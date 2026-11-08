"""Part 6 Task 6: history search UI and record access — scope at the route and the repository."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from wispralt.main import create_app
from wispralt.web.templates_env import highlight

if TYPE_CHECKING:
    from wispralt.accounts.store import User
    from wispralt.jobs.store import JobStore

    from .conftest import MakeUser, Wire


@pytest.fixture
def client(wire: Wire) -> TestClient:
    return TestClient(wire(create_app()), base_url="https://testserver")


@pytest.fixture
def team(make_user: MakeUser) -> dict[str, tuple[User, dict[str, str]]]:
    out = {}
    for name, role in (("dana", "admin"), ("alice", "employee"), ("bob", "employee")):
        user, token = make_user(role=role, label=name)
        out[name] = (user, {"Authorization": f"Bearer {token}"})
    return out


def _dictate(store: JobStore, user_id: int, text: str, created_at: float | None = None) -> str:
    store.insert_dictation(
        user_id=user_id, text=text, duration_ms=500, client_app_version=None, dedup_id=None,
        created_at=created_at,
    )
    with store.db.read() as con:
        return str(con.execute("SELECT MAX(id) FROM dictations").fetchone()[0])


def test_highlight_escapes_before_marking() -> None:
    out = highlight("<b>x</b> \x02roadmap\x03 & more")
    assert str(out) == "&lt;b&gt;x&lt;/b&gt; <mark>roadmap</mark> &amp; more"


def test_search_finds_and_highlights_only_my_rows(
    client: TestClient, team, job_store: JobStore
) -> None:
    alice, a_headers = team["alice"]
    bob, _ = team["bob"]
    _dictate(job_store, alice.id, "the quarterly roadmap review")
    _dictate(job_store, bob.id, "bob's secret roadmap")
    resp = client.get("/me/history", params={"q": "roadm"}, headers=a_headers)
    assert resp.status_code == 200
    assert "<mark>roadmap</mark>" in resp.text and "quarterly" in resp.text
    assert "secret" not in resp.text


def test_snippet_with_script_renders_escaped(client: TestClient, team, job_store: JobStore) -> None:
    alice, headers = team["alice"]
    _dictate(job_store, alice.id, "<script>alert(1)</script> roadmap")
    resp = client.get("/me/history", params={"q": "roadmap"}, headers=headers)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
    assert "<script>alert(1)" not in resp.text


def test_a_short_query_shows_the_plain_list(client: TestClient, team, job_store: JobStore) -> None:
    alice, headers = team["alice"]
    _dictate(job_store, alice.id, "roadmap notes")
    resp = client.get("/me/history", params={"q": "r"}, headers=headers)
    assert resp.status_code == 200 and "roadmap notes" in resp.text and "<mark>" not in resp.text


def test_search_pages_with_offset(
    client: TestClient, team, job_store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wispralt.routes import me

    monkeypatch.setattr(me.settings, "history_page_size", 2)
    alice, headers = team["alice"]
    now = time.time()
    for i in range(3):
        _dictate(job_store, alice.id, f"roadmap item {i}", created_at=now - i)
    first = client.get("/me/history", params={"q": "roadmap"}, headers=headers)
    assert first.text.count("<mark>roadmap</mark>") == 2
    vals = re.search(r"id=\"load-more\"[^>]*hx-vals='([^']+)'", first.text).group(1)
    assert '"offset": 2' in vals and '"q": "roadmap"' in vals
    more = client.get("/me/history", params={"q": "roadmap", "offset": 2}, headers=headers)
    assert more.text.count("<mark>roadmap</mark>") == 1 and "item 2" in more.text
    assert 'id="load-more"' not in more.text


def test_since_until_window_overrides_the_range(client: TestClient, team, job_store: JobStore) -> None:
    alice, headers = team["alice"]
    now = time.time()
    _dictate(job_store, alice.id, "old entry", created_at=now - 5 * 86_400)
    _dictate(job_store, alice.id, "new entry", created_at=now - 60)
    resp = client.get(
        "/me/history", params={"since": now - 6 * 86_400, "until": now - 4 * 86_400}, headers=headers
    )
    assert "old entry" in resp.text and "new entry" not in resp.text


def test_employee_cannot_open_another_users_record(
    client: TestClient, team, job_store: JobStore
) -> None:
    _alice, a_headers = team["alice"]
    bob, _ = team["bob"]
    did = _dictate(job_store, bob.id, "bob only")
    for extra in ({}, {"HX-Request": "true"}):
        resp = client.get(f"/me/history/dictation/{did}", headers={**a_headers, **extra})
        assert resp.status_code == 404
    assert client.get(f"/me/history/dictation/{did}/download/txt", headers=a_headers).status_code == 404
    assert client.get("/me/history/bogus/1", headers=a_headers).status_code == 404


def test_own_record_page_and_htmx_partial(client: TestClient, team, job_store: JobStore) -> None:
    alice, headers = team["alice"]
    did = _dictate(job_store, alice.id, "my full text")
    page = client.get(f"/me/history/dictation/{did}", headers=headers)
    assert page.status_code == 200 and 'class="sidebar"' in page.text and "my full text" in page.text
    part = client.get(f"/me/history/dictation/{did}", headers={**headers, "HX-Request": "true"})
    assert part.text.lstrip().startswith("<tr")


def test_employee_is_forbidden_on_team_records(client: TestClient, team, job_store: JobStore) -> None:
    _alice, headers = team["alice"]
    bob, _ = team["bob"]
    did = _dictate(job_store, bob.id, "bob only")
    assert client.get("/admin/records", headers=headers).status_code == 403
    assert client.get("/admin/records", params={"q": "bob"}, headers=headers).status_code == 403
    assert client.get(f"/admin/records/dictation/{did}", headers=headers).status_code == 403


def test_admin_sees_team_hits_with_owner_names(client: TestClient, team, job_store: JobStore) -> None:
    _admin, headers = team["dana"]
    alice, _ = team["alice"]
    bob, _ = team["bob"]
    _dictate(job_store, alice.id, "alice roadmap")
    did = _dictate(job_store, bob.id, "bob roadmap")
    resp = client.get("/admin/records", params={"q": "roadmap"}, headers=headers)
    assert resp.status_code == 200 and resp.text.count("<mark>roadmap</mark>") == 2
    assert ">alice</a>" in resp.text and ">bob</a>" in resp.text
    only_bob = client.get("/admin/records", params={"q": "roadmap", "user_id": bob.id}, headers=headers)
    assert ">alice</a>" not in only_bob.text
    listing = client.get("/admin/records", headers=headers)
    assert "alice roadmap" in listing.text and "bob roadmap" in listing.text
    record = client.get(f"/admin/records/dictation/{did}", headers=headers)
    assert record.status_code == 200 and "bob roadmap" in record.text
    assert "Admins can read every team transcript" in record.text
    assert client.get("/admin/records/dictation/999999", headers=headers).status_code == 404


def test_deleted_records_are_hidden_everywhere(client: TestClient, team, job_store: JobStore) -> None:
    _admin, admin_headers = team["dana"]
    alice, headers = team["alice"]
    did = _dictate(job_store, alice.id, "regrettable roadmap")
    assert job_store.soft_delete_history_row(alice.id, "dictation", did)
    assert "<mark>" not in client.get("/me/history", params={"q": "roadmap"}, headers=headers).text
    assert "<mark>" not in client.get("/admin/records", params={"q": "roadmap"}, headers=admin_headers).text
    assert client.get(f"/admin/records/dictation/{did}", headers=admin_headers).status_code == 404


def test_integration_key_is_forbidden_on_history(client: TestClient, make_user: MakeUser) -> None:
    _u, token = make_user(kind="integration")
    assert client.get("/me/history", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_repository_scope_for_records(job_store: JobStore, make_user: MakeUser) -> None:
    alice, _ = make_user()
    bob, _ = make_user()
    did = _dictate(job_store, bob.id, "bob text")
    assert job_store.get_record_scoped("dictation", did, viewer_id=alice.id, is_admin=False) is None
    assert job_store.get_record_scoped("dictation", did, viewer_id=bob.id, is_admin=False) is not None
    assert job_store.get_record_scoped("dictation", did, viewer_id=alice.id, is_admin=True) is not None
    assert job_store.get_record_scoped("nope", did, viewer_id=bob.id, is_admin=True) is None
