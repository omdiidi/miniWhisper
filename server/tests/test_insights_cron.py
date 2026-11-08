"""
test_insights_cron.py — the weekly insights cron on the local DB, with a stub
``InsightsClient`` (no network).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from wispralt.config import settings
from wispralt.insights import cron
from wispralt.insights.client import InsightsResponse
from wispralt.insights.timewindow import iso_week_epoch_bounds

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore
    from wispralt.jobs.store import JobStore

    from .conftest import MakeUser

WEEK = (2026, 40)


class _StubClient:
    model = "stub/model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def analyze(self, *, system_prompt: str, user_prompt: str, **_: Any) -> InsightsResponse:
        self.prompts.append(user_prompt)
        body = {"digest": "d", "action_items": [], "decisions": [], "blockers": []}
        return InsightsResponse(
            content=json.dumps(body), input_tokens=10, output_tokens=5, cost_usd=0.01
        )


async def test_file_only_week_produces_a_person_insight(
    job_store: JobStore, accounts: AccountStore, make_user: MakeUser
) -> None:
    alice, _ = make_user(label="alice")
    idle, _ = make_user(label="idle")
    week_start, _week_end = iso_week_epoch_bounds(*WEEK, settings.insights_timezone)
    jid = job_store.create("/tmp/meeting.m4a", user_id=alice.id)
    assert job_store.set_running(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="we shipped it")
    with job_store.db.write() as con:
        con.execute("UPDATE jobs SET created_at=? WHERE id=?", (week_start + 3600, jid))

    client = _StubClient()
    app = SimpleNamespace(
        state=SimpleNamespace(job_store=job_store, accounts=accounts, insights_client=client)
    )
    await cron.run_weekly_insights(app, iso_override=WEEK)

    assert job_store.get_weekly_insight_person(alice.id, *WEEK) is not None  # no dictations needed
    assert job_store.get_weekly_insight_person(idle.id, *WEEK) is None
    assert job_store.get_weekly_insight_team(*WEEK) is not None
    assert "1 file transcriptions" in client.prompts[0]


async def test_openrouter_error_text_never_reaches_the_error_or_logs(
    job_store: JobStore, accounts: AccountStore, make_user: MakeUser, caplog
) -> None:
    import logging

    import httpx
    import pytest

    from wispralt.insights.client import InsightsClient, InsightsError

    marker = "SECRET-TRANSCRIPT-ECHO-7f3a"
    alice, _ = make_user(label="alice")
    week_start, _ = iso_week_epoch_bounds(*WEEK, settings.insights_timezone)
    jid = job_store.create("/tmp/a.m4a", user_id=alice.id)
    assert job_store.set_running(jid)
    assert job_store.set_done_if_not_cancelled(jid, "single", transcript_text="hello")
    with job_store.db.write() as con:
        con.execute("UPDATE jobs SET created_at=? WHERE id=?", (week_start + 60, jid))

    client = InsightsClient(api_key="k", model="m", timeout_s=5.0)
    await client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://openrouter.invalid/api/v1",
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(500, text=f"upstream said: {marker}")
        ),
    )
    try:
        with pytest.raises(InsightsError) as exc:
            await client.analyze(system_prompt="s", user_prompt="u")
        assert marker not in str(exc.value) and exc.value.status_code == 500

        app = SimpleNamespace(
            state=SimpleNamespace(job_store=job_store, accounts=accounts, insights_client=client)
        )
        with caplog.at_level(logging.DEBUG):
            await cron.run_weekly_insights(app, iso_override=WEEK)
    finally:
        await client._client.aclose()

    assert not any(marker in r.getMessage() for r in caplog.records)
    failures = [r.getMessage() for r in caplog.records if "Person insights failed" in r.getMessage()]
    assert failures == [f"Person insights failed for user_id={alice.id}: InsightsError status=500"]
