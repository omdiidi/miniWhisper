"""Part 6 Task 9: insight runs, the hourly scheduler, settings, rerun/backfill, estimate.

The OpenRouter client is a stub (no network, no spend).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.testclient import TestClient

from wispralt.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from wispralt.insights import cron, timewindow
from wispralt.insights import settings as insights_settings
from wispralt.insights.client import INSIGHTS_PRICING_PER_1K, InsightsResponse, RateLimitedError
from wispralt.insights.timewindow import iso_week_epoch_bounds, last_full_iso_week
from wispralt.main import create_app
from wispralt.settings_store import SettingsStore

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from wispralt.accounts.store import AccountStore
    from wispralt.jobs.store import JobStore

    from .conftest import MakeUser, Wire

TZ = insights_settings.resolve_timezone(None)


class Stub:
    """Valid v2 answers citing the refs it was given. ``cost`` per call; ``fail_with``
    raises on the Nth call (1-based); ``on_call(n)`` runs before answering."""

    model = "stub/model"

    def __init__(self, cost: float = 0.01, fail_with: tuple[int, Exception] | None = None,
                 on_call=None) -> None:  # noqa: ANN001
        self.calls = 0
        self.cost = cost
        self.fail_with = fail_with
        self.on_call = on_call

    async def analyze(self, *, system_prompt: str, user_prompt: str, **_: Any) -> InsightsResponse:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.fail_with and self.fail_with[0] == self.calls:
            raise self.fail_with[1]
        refs = re.findall(r"\[([df]:[^\]]+)\]", user_prompt) or re.findall(r'"([df]:[^"]+)"', user_prompt)
        item = {"text": "did things", "sources": refs[:1]}
        body = {"digest": [item], "themes": [{"label": "work", "sources": refs[:1]}]}
        return InsightsResponse(content=json.dumps(body), input_tokens=10, output_tokens=5,
                                cost_usd=self.cost)


def _ai_on(store: SettingsStore, cap: float = 8.0) -> None:
    insights_settings.save(store, enabled=True, acknowledge=True, model="stub/model",
                           cap_30d_usd=cap, timezone=None)


def _file(store: JobStore, uid: int, text: str, at: float) -> str:
    jid = store.create("/tmp/x.wav", user_id=uid)
    assert store.set_running(jid)
    assert store.set_done_if_not_cancelled(jid, "single", transcript_text=text)
    with store.db.write() as con:
        con.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (at, jid))
    return jid


@pytest.fixture
def settings_store(db) -> SettingsStore:  # noqa: ANN001
    return SettingsStore(db)


@pytest.fixture
def week() -> tuple[int, int]:
    return last_full_iso_week(TZ)


@pytest.fixture
def busy_people(make_user: MakeUser, job_store: JobStore, week) -> list:  # noqa: ANN001
    start, _ = iso_week_epoch_bounds(*week, TZ)
    people = [make_user(label=name)[0] for name in ("alice", "bob", "carol")]
    for i, p in enumerate(people):
        _file(job_store, p.id, f"{p.label} worked on the launch plan " * 10, start + 60 * (i + 1))
    return people


def _app(job_store: JobStore, accounts: AccountStore, store: SettingsStore, client: Any) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        job_store=job_store, accounts=accounts, settings_store=store, insights_client=client,
        weekly_insights_lock=asyncio.Lock(),
    ))


# ── enablement and the scheduler ─────────────────────────────────────────────


async def test_ai_disabled_is_never_due_and_never_calls_the_client(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    stub = Stub()
    app = _app(job_store, accounts, settings_store, stub)
    assert await cron.is_due(app) is False
    assert await cron.run_weekly_insights(app, week, trigger="manual") is None
    assert stub.calls == 0
    [run] = job_store.recent_insight_runs()
    assert (run["status"], run["error_class"]) == ("skipped", "ai_disabled")


def test_enabled_without_an_ack_is_off_and_save_refuses(settings_store: SettingsStore) -> None:
    settings_store.set_many({"insights_enabled": "1"})  # hand-edited DB, no disclosure ack
    assert insights_settings.load(settings_store).enabled is False
    with pytest.raises(ValueError, match="disclosure"):
        insights_settings.save(settings_store, enabled=True, acknowledge=False, model="m",
                               cap_30d_usd=1, timezone=None)


async def test_a_due_week_runs_once_and_the_next_tick_is_a_no_op(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    _ai_on(settings_store)
    stub = Stub()
    app = _app(job_store, accounts, settings_store, stub)
    assert await cron.is_due(app) is True
    outcome = await cron.run_weekly_insights(app, trigger="scheduled")
    assert (outcome.people_total, outcome.people_done) == (3, 3)
    calls = stub.calls
    assert await cron.is_due(app) is False
    [run] = job_store.recent_insight_runs()
    assert (run["status"], run["trigger"], (run["iso_year"], run["iso_week"])) == (
        "done", "scheduled", week)
    assert stub.calls == calls


async def test_a_failed_scheduled_attempt_retries_only_after_six_hours(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people
) -> None:
    _ai_on(settings_store)
    app = _app(job_store, accounts, settings_store, Stub(fail_with=(1, RateLimitedError())))
    await cron.run_weekly_insights(app, trigger="scheduled")
    now = time.time()
    assert await cron.is_due(app, now=now + 60) is False
    assert await cron.is_due(app, now=now + cron.RETRY_AFTER_S + 60) is True


async def test_two_weeks_ahead_the_scheduler_runs_only_the_last_full_week(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people
) -> None:
    _ai_on(settings_store)
    app = _app(job_store, accounts, settings_store, Stub())
    later = time.time() + 14 * 86_400
    assert await cron.is_due(app, now=later)
    await cron.run_weekly_insights(app, trigger="scheduled", now=later)
    runs = job_store.recent_insight_runs()
    assert [(r["iso_year"], r["iso_week"]) for r in runs] == [last_full_iso_week(TZ, later)]


# ── budget, rate limit, progress ─────────────────────────────────────────────


async def test_hitting_the_cap_mid_run_aborts_with_budget(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    _ai_on(settings_store, cap=8.0)
    stub = Stub(cost=5.0)
    outcome = await cron.run_weekly_insights(_app(job_store, accounts, settings_store, stub), week,
                                             trigger="manual")
    assert stub.calls == 2  # 0 < 8 → call; 5 < 8 → call; 10 ≥ 8 → stop before the third
    assert outcome.aborted == "budget"
    [run] = job_store.recent_insight_runs()
    assert (run["status"], run["error_class"], run["people_done"]) == ("aborted", "budget", 2)
    assert run["cost_usd"] == 10.0 and job_store.get_weekly_insight_team(*week) is None


async def test_rate_limit_aborts_the_run(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    _ai_on(settings_store)
    stub = Stub(fail_with=(2, RateLimitedError(retry_after_s=30)))
    await cron.run_weekly_insights(_app(job_store, accounts, settings_store, stub), week,
                                   trigger="manual")
    [run] = job_store.recent_insight_runs()
    assert (run["status"], run["error_class"], run["people_done"]) == ("aborted", "rate_limit", 1)
    assert [f["error_class"] for f in run["failures"]] == ["rate_limit"]
    assert stub.calls == 2


async def test_progress_advances_during_the_run(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    _ai_on(settings_store)
    seen: list[tuple[int, int, str]] = []

    def peek(n: int) -> None:
        r = job_store.recent_insight_runs()[0]
        seen.append((r["people_done"], r["people_total"], r["status"]))

    await cron.run_weekly_insights(_app(job_store, accounts, settings_store, Stub(on_call=peek)),
                                   week, trigger="manual")
    assert seen[:3] == [(0, 1, "running"), (1, 2, "running"), (2, 3, "running")]


async def test_a_three_week_backfill_is_three_sequential_runs(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people
) -> None:
    _ai_on(settings_store)
    weeks = cron.last_weeks(3, TZ)
    assert len(set(weeks)) == 3 and weeks[-1] == last_full_iso_week(TZ)
    app = _app(job_store, accounts, settings_store, Stub())
    results = await cron.run_weeks(app, weeks, trigger="backfill")
    assert len(results) == 3
    runs = sorted(job_store.recent_insight_runs(), key=lambda r: r["id"])
    assert [(r["iso_year"], r["iso_week"]) for r in runs] == weeks
    assert {r["trigger"] for r in runs} == {"backfill"}
    for a, b in zip(runs, runs[1:], strict=False):
        assert a["finished_at"] <= b["started_at"]  # one after another, never overlapping


async def test_a_held_lock_refuses_a_second_run(
    job_store: JobStore, accounts: AccountStore, settings_store: SettingsStore, busy_people, week
) -> None:
    _ai_on(settings_store)
    app = _app(job_store, accounts, settings_store, Stub())
    async with app.state.weekly_insights_lock:
        assert await cron.run_weeks(app, [week], trigger="manual") is None
    assert job_store.recent_insight_runs() == []


def test_estimate_is_words_times_1_35_over_1000_times_the_input_rate(
    job_store: JobStore, accounts: AccountStore, busy_people, week
) -> None:
    est = cron.estimate_cost(job_store, accounts, [week], model="x-ai/grok-4.3", tz_name=TZ)
    words = 3 * 60  # three people, 60 words each
    assert est["words"] == words
    assert est["usd"] == round(words * 1.35 / 1000 * INSIGHTS_PRICING_PER_1K["x-ai/grok-4.3"]["input"], 4)


# ── timezone fallback ────────────────────────────────────────────────────────


def test_timezone_falls_back_to_utc_when_localtime_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timewindow, "LOCALTIME", tmp_path / "missing")
    assert timewindow.host_timezone() == "UTC"
    bogus = tmp_path / "localtime"
    os.symlink("/nowhere/zoneinfo/Not/AZone", bogus)
    monkeypatch.setattr(timewindow, "LOCALTIME", bogus)
    assert timewindow.host_timezone() == "UTC"
    good = tmp_path / "good"
    os.symlink("/usr/share/zoneinfo/Europe/Berlin", good)
    monkeypatch.setattr(timewindow, "LOCALTIME", good)
    assert timewindow.host_timezone() == "Europe/Berlin"
    assert insights_settings.resolve_timezone("Asia/Tokyo") == "Asia/Tokyo"


# ── portal routes ────────────────────────────────────────────────────────────


@pytest.fixture
def app(wire: Wire) -> FastAPI:
    app = wire(create_app())
    app.state.weekly_insights_lock = asyncio.Lock()
    return app


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _admin_cookie_client(app: FastAPI, token: str) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set(SESSION_COOKIE_NAME, token)
    c.cookies.set(CSRF_COOKIE_NAME, "tok")
    return c


def test_settings_require_the_disclosure_and_csrf(
    app: FastAPI, make_user: MakeUser, settings_store: SettingsStore
) -> None:
    _a, token = make_user(role="admin")
    _e, emp = make_user()
    form = {"enabled": "1", "model": "x-ai/grok-4.3", "cap_30d_usd": "5", "timezone": "",
            "csrf_token": "tok"}
    c = _admin_cookie_client(app, token)
    page = c.get("/admin/settings")
    assert page.status_code == 200 and "Disclosure" in page.text and "not set" in page.text
    form["csrf_token"] = _csrf(page.text)  # the page set a fresh CSRF cookie
    no_ack = c.post("/admin/settings", data=form)
    assert no_ack.status_code == 400 and "disclosure" in no_ack.text  # no ack
    form["csrf_token"] = _csrf(no_ack.text)  # every re-render mints a fresh token
    assert insights_settings.load(settings_store).enabled is False
    no_csrf = {k: v for k, v in form.items() if k != "csrf_token"}
    assert c.post("/admin/settings", data={**no_csrf, "acknowledge": "1"}).status_code == 403
    assert TestClient(app, base_url="https://testserver").post(
        "/admin/settings", data=form, headers={"Authorization": f"Bearer {emp}"}).status_code == 403
    bad_tz = c.post("/admin/settings", data={**form, "acknowledge": "1", "timezone": "Mars/Base"})
    assert bad_tz.status_code == 400 and "Unknown timezone" in bad_tz.text
    form["csrf_token"] = _csrf(bad_tz.text)
    ok = c.post("/admin/settings", data={**form, "acknowledge": "1"}, follow_redirects=False)
    assert ok.status_code == 303
    cfg = insights_settings.load(settings_store)
    assert cfg.enabled and cfg.disclosure_ack_at and cfg.cap_30d_usd == 5.0
    # Turning it off later keeps the ack; turning it back on needs no new ack.
    assert c.post("/admin/settings", data={**form, "enabled": ""}, follow_redirects=False).status_code == 303
    assert insights_settings.load(settings_store).enabled is False
    assert c.post("/admin/settings", data=form, follow_redirects=False).status_code == 303


def test_runs_post_is_csrf_guarded_admin_only_and_refused_while_ai_is_off(
    app: FastAPI, make_user: MakeUser, settings_store: SettingsStore
) -> None:
    _a, token = make_user(role="admin")
    _e, emp = make_user()
    c = _admin_cookie_client(app, token)
    assert c.post("/admin/data/runs", data={"weeks": "1"}).status_code == 403
    assert TestClient(app, base_url="https://testserver").post(
        "/admin/data/runs", data={"weeks": "1"}, headers={"Authorization": f"Bearer {emp}"}
    ).status_code == 403
    off = c.post("/admin/data/runs", data={"weeks": "1", "csrf_token": "tok"})
    assert off.status_code == 409 and "off" in off.text


async def test_estimate_then_confirm_starts_a_backfill_and_the_partial_shows_progress(
    app: FastAPI, make_user: MakeUser, settings_store: SettingsStore, busy_people
) -> None:
    _a, token = make_user(role="admin")
    _ai_on(settings_store)
    app.state.insights_client = Stub()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
        ac.cookies.set(SESSION_COOKIE_NAME, token)
        ac.cookies.set(CSRF_COOKIE_NAME, "tok")
        page = await ac.get("/admin/data")
        assert 'id="runs"' in page.text and 'hx-post="/admin/data/runs"' in page.text
        csrf = _csrf(page.text)
        est = await ac.post("/admin/data/runs", data={"weeks": "2", "csrf_token": csrf})
        assert est.status_code == 200 and "Estimated input cost" in est.text
        assert app.state.job_store.recent_insight_runs() == []  # nothing started yet
        vals = json.loads(re.search(r"hx-vals='([^']+)'", est.text).group(1))
        assert vals == {"weeks": 2, "confirm": "1", "csrf_token": csrf}
        started = await ac.post("/admin/data/runs", data={k: str(v) for k, v in vals.items()})
        assert started.status_code == 200 and "Started 2 weeks" in started.text
        await asyncio.gather(*app.state.insights_run_tasks)
        runs = app.state.job_store.recent_insight_runs()
        assert [r["trigger"] for r in runs] == ["backfill", "backfill"]
        partial = await ac.get("/admin/data/runs/current")
        assert partial.status_code == 200 and "3 / 3" in partial.text
        assert CSRF_COOKIE_NAME not in partial.headers.get("set-cookie", "")
        assert 'hx-trigger="every 2s"' not in partial.text  # nothing running: polling stops
