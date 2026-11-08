"""Part 6 Task 8: AI insights v2 — every claim cites records in the prompt, checked server-side.

The OpenRouter client is a scripted stub: no network, no spend.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient

from wispralt.insights import cron, sources
from wispralt.insights import settings as insights_settings
from wispralt.insights.client import InsightsResponse
from wispralt.insights.timewindow import iso_week_epoch_bounds, last_full_iso_week
from wispralt.main import create_app
from wispralt.settings_store import SettingsStore

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore, User
    from wispralt.jobs.store import JobStore

    from .conftest import MakeUser, Wire

WEEK = (2026, 40)
TZ = insights_settings.resolve_timezone(None)
SECRET = "zebra-quartz-transcript-marker"


class ScriptedClient:
    """Answers each analyze() with the next scripted body; a callable gets the prompt."""

    model = "stub/model"

    def __init__(self, *bodies: Any) -> None:
        self.bodies = list(bodies)
        self.prompts: list[str] = []

    async def analyze(self, *, system_prompt: str, user_prompt: str, **_: Any) -> InsightsResponse:
        self.prompts.append(user_prompt)
        body = self.bodies.pop(0)
        if callable(body):
            body = body(user_prompt)
        content = body if isinstance(body, str) else json.dumps(body)
        return InsightsResponse(content=content, input_tokens=10, output_tokens=5, cost_usd=0.01)


def _refs(prompt: str) -> list[str]:
    return re.findall(r"\[([df]:[^\]]+)\]", prompt)


def _dictate(store: JobStore, uid: int, text: str, at: float) -> str:
    store.insert_dictation(user_id=uid, text=text, duration_ms=1, client_app_version=None,
                           dedup_id=None, created_at=at)
    with store.db.read() as con:
        return str(con.execute("SELECT MAX(id) FROM dictations").fetchone()[0])


def _file(store: JobStore, uid: int, text: str, at: float) -> str:
    jid = store.create("/tmp/x.wav", user_id=uid)
    assert store.set_running(jid)
    assert store.set_done_if_not_cancelled(jid, "single", transcript_text=text)
    with store.db.write() as con:
        con.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (at, jid))
    return jid


def _app(job_store: JobStore, accounts: AccountStore, client: ScriptedClient) -> SimpleNamespace:
    store = SettingsStore(job_store.db)
    insights_settings.save(store, enabled=True, acknowledge=True, model="stub/model",
                           cap_30d_usd=8.0, timezone=None)
    return SimpleNamespace(state=SimpleNamespace(job_store=job_store, accounts=accounts,
                                                 insights_client=client, settings_store=store))


@pytest.fixture
def week_start() -> float:
    return iso_week_epoch_bounds(*WEEK, TZ)[0]


@pytest.fixture
def people(make_user: MakeUser) -> tuple[User, User]:
    return make_user(label="alice")[0], make_user(label="bob")[0]


# ── unit: assemble / validate ────────────────────────────────────────────────


def test_assemble_tags_records_and_caps_words() -> None:
    recs = [
        {"kind": "dictation", "row_id": "7", "text": "one two three"},
        {"kind": "file", "row_id": "ab-12", "text": "four five six seven"},
        {"kind": "dictation", "row_id": "9", "text": "never included"},
    ]
    a = sources.assemble(recs, cap=5)
    assert a.blob == "[d:7] one two three\n---\n[f:ab-12] four five"
    assert a.allowed == {"d:7", "f:ab-12"} and a.total_words == 9
    assert a.texts["f:ab-12"] == "four five six seven"  # quotes check the full record


def test_validate_person_drops_invented_refs_and_bad_quotes() -> None:
    a = sources.assemble([{"kind": "dictation", "row_id": "1", "text": "We ship Friday."}], cap=100)
    out = sources.validate_person(
        {
            "digest": [{"text": "shipping", "sources": ["d:1", "d:999"]}],
            "action_items": [{"text": "invented", "sources": ["d:999"]}, {"text": "no refs"}],
            "topics": [{"label": "release", "sources": ["d:1"]}],
            "quotable_line": {"text": "We ship Monday.", "source": "d:1"},
            "filler_word_count": 99, "pct_time": 0.5,
        },
        a,
    )
    assert out["schema_version"] == 2
    assert out["digest"] == [{"text": "shipping", "sources": ["d:1"]}]
    assert out["action_items"] == [] and out["quotable_line"] is None
    assert "filler_word_count" not in out and "pct_time" not in out
    ok = sources.validate_person({"quotable_line": {"text": "We ship Friday.", "source": "d:1"}}, a)
    assert ok["quotable_line"] == {"text": "We ship Friday.", "source": "d:1"}


@pytest.mark.parametrize("raw", [[], "x", {"digest": "not a list"}, {"topics": [{"sources": "d:1"}]}])
def test_wrong_shapes_raise(raw: Any) -> None:
    a = sources.assemble([], cap=10)
    with pytest.raises(sources.InsightShapeError):
        sources.validate_person(raw, a)


def test_validate_team_keeps_only_refs_from_the_person_insights() -> None:
    out = sources.validate_team(
        {"themes": [{"label": "a", "sources": ["d:1", "d:2"]}, {"label": "b", "sources": ["d:3"]}],
         "wow_delta": "+3", "tool_mentions": {"x": 1}},
        allowed={"d:1"},
    )
    assert out["themes"] == [{"label": "a", "sources": ["d:1"]}]
    assert "wow_delta" not in out and "tool_mentions" not in out


# ── the run: refs checked against what was actually in the prompt ────────────


async def test_run_drops_other_users_refs_and_team_refs_outside_digests(
    job_store: JobStore, accounts: AccountStore, people, week_start: float,
    caplog: pytest.LogCaptureFixture,
) -> None:
    alice, bob = people
    a1 = _file(job_store, alice.id, f"Alice notes {SECRET}. We decided to ship.", week_start + 60)
    b1 = _file(job_store, bob.id, "Bob private thoughts", week_start + 120)

    def alice_answer(prompt: str) -> dict:
        [mine] = _refs(prompt)
        return {
            "digest": [{"text": "alice shipped", "sources": [mine]}],
            "decisions": [{"text": "leaks bob", "sources": [f"f:{b1}"]}],
            "action_items": [{"text": "mixed", "sources": [mine, f"f:{b1}"]}],
            "quotable_line": {"text": "We decided to ship.", "source": mine},
        }

    def bob_answer(prompt: str) -> dict:
        [mine] = _refs(prompt)
        return {"digest": [{"text": "bob thought", "sources": [mine]}]}

    team = {
        "themes": [{"label": "shipping", "sources": [f"f:{a1}"]},
                   {"label": "made up", "sources": ["d:424242"]}],
        "knowledge_gaps": [{"text": "uncited"}],
    }
    def person_answer(prompt: str) -> dict:
        return alice_answer(prompt) if "Person: alice" in prompt else bob_answer(prompt)

    client = ScriptedClient(person_answer, person_answer, team)
    with caplog.at_level(logging.DEBUG):
        outcome = await cron.run_weekly_insights(_app(job_store, accounts, client), WEEK,
                                                 trigger="manual")
    assert (outcome.people_total, outcome.people_done, outcome.failures) == (2, 2, [])

    alice_insight = job_store.get_weekly_insight_person(alice.id, *WEEK)
    assert alice_insight["decisions"] == []  # bob's record was never in alice's prompt
    assert alice_insight["action_items"] == [{"text": "mixed", "owner": None, "sources": [f"f:{a1}"]}]
    assert alice_insight["quotable_line"]["source"] == f"f:{a1}"
    team_insight = job_store.get_weekly_insight_team(*WEEK)
    assert [t["label"] for t in team_insight["themes"]] == ["shipping"]
    assert team_insight["knowledge_gaps"] == []

    assert all("api_key_id" not in p and "user_id" not in p for p in client.prompts)
    assert any(SECRET in p for p in client.prompts)  # it did go to the (stub) model …
    assert not any(SECRET in r.getMessage() for r in caplog.records)  # … but never to a log


async def test_malformed_or_misshapen_json_is_a_person_failure(
    job_store: JobStore, accounts: AccountStore, people, week_start: float
) -> None:
    alice, bob = people
    _file(job_store, alice.id, "alice text", week_start + 60)
    _file(job_store, bob.id, "bob text", week_start + 60)
    client = ScriptedClient("not json {", {"digest": [{"text": "x", "sources": "f:abc"}]})
    outcome = await cron.run_weekly_insights(_app(job_store, accounts, client), WEEK,
                                             trigger="manual")
    assert sorted(f["error_class"] for f in outcome.failures) == ["json_decode", "schema"]
    assert {f["user_id"] for f in outcome.failures} == {alice.id, bob.id}
    assert outcome.people_done == 0 and job_store.get_weekly_insight_team(*WEEK) is None


# ── render: deleted sources, stale badge, legacy rows, recurring topics ──────


def _store(job_store: JobStore, user_id: int | None, week: tuple[int, int], insight: dict) -> None:
    job_store.upsert_weekly_insight(iso_year=week[0], iso_week=week[1], user_id=user_id,
                                    insight=insight, input_tokens=1, output_tokens=1,
                                    cost_usd=0.01, model="stub/model")


@pytest.fixture
def page_client(wire: Wire) -> TestClient:
    return TestClient(wire(create_app()), base_url="https://testserver")


def test_pages_link_sources_hide_deleted_mark_stale_and_recurring(
    page_client: TestClient, job_store: JobStore, make_user: MakeUser
) -> None:
    admin, admin_token = make_user(role="admin", label="dana")
    alice, token = make_user(label="alice")
    week = last_full_iso_week(TZ)
    start, _end = iso_week_epoch_bounds(*week, TZ)
    kept = _dictate(job_store, alice.id, "kept record", start + 60)
    gone = _dictate(job_store, alice.id, "to be deleted", start + 120)
    prev = (week[0], week[1] - 1) if week[1] > 1 else (week[0] - 1, 52)
    _store(job_store, alice.id, prev, {"schema_version": 2, "topics": [{"label": "Roadmap", "sources": []}]})
    _store(job_store, alice.id, week, {
        "schema_version": 2,
        "digest": [{"text": "visible item", "sources": [f"d:{kept}"]}],
        "blockers": [{"text": "hidden item", "sources": [f"d:{gone}"]}],
        "topics": [{"label": "roadmap", "sources": [f"d:{kept}"]}],
        "quotable_line": None,
    })
    assert job_store.soft_delete_history_row(alice.id, "dictation", gone)

    me = page_client.get("/me/insights", headers={"Authorization": f"Bearer {token}"}).text
    assert "AI interpretation" in me and "visible item" in me and "hidden item" not in me
    assert f'href="/me/history/dictation/{kept}"' in me
    assert "seen 2 weeks" in me
    assert "added after it was generated" not in me

    _dictate(job_store, alice.id, "late arrival", start + 3600)
    with job_store.db.write() as con:  # the insight was generated before that record
        con.execute("UPDATE weekly_insights SET generated_at = ? WHERE scope_key = ?",
                    (start + 1800, f"user:{alice.id}"))
    me = page_client.get("/me/insights", headers={"Authorization": f"Bearer {token}"}).text
    assert "added after it was generated" in me

    admin_page = page_client.get("/admin/data", params={"user_id": alice.id},
                                 headers={"Authorization": f"Bearer {admin_token}"}).text
    assert f'href="/admin/records/dictation/{kept}"' in admin_page and "hidden item" not in admin_page


def test_legacy_rows_and_ai_off(
    page_client: TestClient, job_store: JobStore, make_user: MakeUser
) -> None:
    alice, token = make_user(label="alice")
    headers = {"Authorization": f"Bearer {token}"}
    off = page_client.get("/me/insights", headers=headers).text
    assert "AI interpretation is off" in off
    week = last_full_iso_week(TZ)
    _store(job_store, alice.id, week, {"digest": ["old style"], "filler_word_count": 3})
    legacy = page_client.get("/me/insights", headers=headers).text
    assert "older format" in legacy and "old style" not in legacy


def test_refs_exist_respects_scope(job_store: JobStore, people) -> None:
    alice, bob = people
    a = _dictate(job_store, alice.id, "a", 1.0)
    b = _dictate(job_store, bob.id, "b", 1.0)
    refs = {f"d:{a}", f"d:{b}", "d:999999", "f:nope", "x:1"}
    assert job_store.refs_exist(refs, viewer_id=alice.id, is_admin=False) == {f"d:{a}"}
    assert job_store.refs_exist(refs, viewer_id=alice.id, is_admin=True) == {f"d:{a}", f"d:{b}"}
