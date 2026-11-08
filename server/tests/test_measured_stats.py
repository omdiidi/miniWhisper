"""Part 6 Task 7: measured stats from DB rows (no AI), and the pages that show them."""

from __future__ import annotations

import html
import re
import time
import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from wispralt.config import settings
from wispralt.insights import measured
from wispralt.main import create_app
from wispralt.usage.events import UsageEvent

if TYPE_CHECKING:
    from wispralt.accounts.store import User
    from wispralt.jobs.store import JobStore
    from wispralt.usage.store import UsageStore

    from .conftest import MakeUser, Wire

TZ = "America/New_York"
_Z = ZoneInfo(TZ)


def _ts(*args: int) -> float:
    return datetime(*args, tzinfo=_Z).timestamp()


NOW = _ts(2026, 11, 4, 18, 0)  # a Wednesday, 18:00 New York


def _dictate(store: JobStore, uid: int, text: str, at: float, ms: int = 100) -> str:
    store.insert_dictation(user_id=uid, text=text, duration_ms=ms, client_app_version=None,
                           dedup_id=None, created_at=at)
    with store.db.read() as con:
        return str(con.execute("SELECT MAX(id) FROM dictations").fetchone()[0])


def _file(store: JobStore, uid: int, text: str, at: float, audio_s: float) -> str:
    jid = store.create("/tmp/x.wav", user_id=uid)
    assert store.set_running(jid)
    assert store.set_done_if_not_cancelled(jid, "single", transcript_text=text)
    store.update_audio_duration(jid, audio_s)
    with store.db.write() as con:
        con.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (at, jid))
    return jid


@pytest.fixture
def seeded(job_store: JobStore, make_user: MakeUser, usage: UsageStore) -> User:
    alice, _ = make_user(label="alice")
    bob, _ = make_user(label="bob")
    _dictate(job_store, alice.id, "Um, so like, you know, uh the plan", _ts(2026, 11, 2, 9, 15))  # 8 w
    _dictate(job_store, alice.id, "Ummm uhh fine", _ts(2026, 11, 3, 23, 30))  # 3 words
    gone = _dictate(job_store, alice.id, "um um um deleted", _ts(2026, 11, 3, 10, 0))
    assert job_store.soft_delete_history_row(alice.id, "dictation", gone)
    _dictate(job_store, alice.id, "three weeks ago", _ts(2026, 10, 14, 9, 0))  # outside the window
    _file(job_store, alice.id, "meeting notes here today", _ts(2026, 11, 3, 14, 5), audio_s=150.0)
    _dictate(job_store, bob.id, "um bob talks", _ts(2026, 11, 2, 12, 0))
    for ms, status in ((100.0, 200), (300.0, 200), (500.0, 500)):
        usage.record(UsageEvent(user_id=alice.id, ts=NOW - 3600, kind="dictate", status=status,
                                duration_ms=ms))
    return alice


def test_fillers_regex() -> None:
    assert measured.count_fillers(["Um, so like, you know, uh", "UMM uhh unlike umbrella"]) == {
        "um": 2, "uh": 2, "like": 1, "you know": 1, "total": 6,
    }


def test_person_window_matches_the_fixture_exactly(
    job_store: JobStore, usage: UsageStore, seeded: User
) -> None:
    since = _ts(2026, 11, 2, 0, 0)  # this ISO week's Monday
    m = measured.compute(job_store, usage, user_id=seeded.id, since=since, until=NOW, now=NOW,
                         tz_name=TZ)
    assert (m["dictation_count"], m["file_count"]) == (2, 1)  # soft-deleted excluded
    assert m["words"] == 8 + 3 + 4
    assert m["file_audio_minutes"] == 2.5
    assert m["active_days"] == 2  # Mon 2nd and Tue 3rd (23:30 local stays on the 3rd)
    assert m["fillers"] == {"um": 2, "uh": 2, "like": 1, "you know": 1, "total": 6}
    assert m["time_saved_hours"] == round(15 / 40 / 60, 2)
    assert (m["latency_p50_ms"], m["latency_p95_ms"]) == (300.0, pytest.approx(480.0))
    assert m["requests"] == 3 and m["error_rate"] == pytest.approx(1 / 3)
    hours = m["hours"]
    assert (hours[9], hours[23], hours[14], sum(hours)) == (1, 1, 1, 3)
    trend = m["trend"]
    assert len(trend) == 8 and trend[-1]["label"] == "W45" and trend[-1]["count"] == 3
    assert trend[-1]["words"] == 15
    assert [b["count"] for b in trend if b["label"] == "W42"] == [1]  # three weeks ago


def test_team_scope_and_filler_leaderboard(job_store: JobStore, usage: UsageStore, seeded: User) -> None:
    since = _ts(2026, 11, 2, 0, 0)
    m = measured.compute(job_store, usage, user_id=None, since=since, until=NOW, now=NOW, tz_name=TZ)
    assert (m["dictation_count"], m["file_count"]) == (3, 1)
    by_user = measured.fillers_by_user(job_store, since, NOW)
    assert by_user[seeded.id] == 6 and sum(by_user.values()) == 7


def test_trend_buckets_follow_the_timezone() -> None:
    # Monday 00:30 in New York is still Sunday in UTC-5: it belongs to the NY week.
    monday_early = _ts(2026, 11, 2, 0, 30)
    trend = measured.weekly_trend([(monday_early, 5)], NOW, TZ)
    assert trend[-1]["count"] == 1
    trend_utc = measured.weekly_trend([(monday_early, 5)], NOW, "UTC")
    assert trend_utc[-1]["count"] == 1  # 05:30 UTC Monday: same ISO week in UTC too
    sunday_late = _ts(2026, 11, 1, 23, 30)  # Sunday NY = Monday 04:30 UTC
    assert measured.weekly_trend([(sunday_late, 1)], NOW, TZ)[-1]["count"] == 0
    assert measured.weekly_trend([(sunday_late, 1)], NOW, "UTC")[-1]["count"] == 1


# ── pages ────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    return TestClient(wire(create_app()), base_url="https://testserver")


def _tile_href(page: str, label: str) -> str:
    m = re.search(rf'<div class="label"><a href="([^"]+)">{re.escape(label)}</a></div>', page)
    assert m, label
    return html.unescape(m.group(1))


def test_my_insights_renders_measured_with_ai_off_and_tiles_open_their_records(
    client: TestClient, make_user: MakeUser, job_store: JobStore
) -> None:
    alice, token = make_user(label="alice")
    headers = {"Authorization": f"Bearer {token}"}
    now = time.time()
    _dictate(job_store, alice.id, "first uh dictation", now - 3600)
    _dictate(job_store, alice.id, "second dictation", now - 7200)
    _file(job_store, alice.id, "a file transcript", now - 1800, audio_s=60)
    page = client.get("/me/insights", params={"range": "7d"}, headers=headers)
    assert page.status_code == 200
    assert "Measured" in page.text and "estimate, 40 wpm typing baseline" in page.text

    href = _tile_href(page.text, "Dictations")
    url = urllib.parse.urlsplit(href)
    params = dict(urllib.parse.parse_qsl(url.query))
    assert url.path == "/me/history" and params["kind"] == "dictation"
    assert float(params["since"]) == pytest.approx(now - 7 * 86_400, abs=60)
    listed = client.get(href, headers=headers).text
    assert "first uh dictation" in listed and "second dictation" in listed
    assert "a file transcript" not in listed
    assert "a file transcript" in client.get(_tile_href(page.text, "File transcriptions"),
                                             headers=headers).text


def test_admin_data_shows_team_measured_and_leaderboard(
    client: TestClient, make_user: MakeUser, job_store: JobStore
) -> None:
    _admin, token = make_user(role="admin", label="dana")
    alice, _ = make_user(label="alice")
    _dictate(job_store, alice.id, "um uh like words", time.time() - 60)
    headers = {"Authorization": f"Bearer {token}"}
    page = client.get("/admin/data", headers=headers)
    assert page.status_code == 200
    assert _tile_href(page.text, "Words").startswith("/admin/records?")
    assert re.search(r"<td>alice</td>(\s*<td class=\"num\">[^<]*</td>){4}", page.text)
    drill = client.get("/admin/data", params={"user_id": alice.id}, headers=headers)
    href = _tile_href(drill.text, "Words")
    assert f"user_id={alice.id}" in href
    assert "um uh like words" in client.get(href, headers=headers).text
