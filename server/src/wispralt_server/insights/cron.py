"""Weekly LLM insights cron — Phase 2.

Sunday 23:00 mini-local sweep. Walks Supabase users, pulls last-ISO-week
transcripts from the local JobStore, calls OpenRouter once per qualifying
person, then once more for the team aggregate, and upserts results to
``weekly_insights``.

Design invariants:
- Rolling-30d cost guard at $8 (config-driven) — re-checked between users
  using a baseline snapshot + accumulated-run-cost so the same iteration
  doesn't double-count.
- Idempotent on Sunday-night restart: if a person already has a row for
  the target ISO week, skip them (no double OpenRouter charge).
- Concurrency-safe: ``app.state.weekly_insights_lock`` (an ``asyncio.Lock``
  set in lifespan) serializes scheduled fires vs catchup so a catchup that
  overlaps the schedule doesn't double-bill OpenRouter.
- Daemon loop wraps run_weekly_insights in a broad-except so a single
  failure never kills the task.
- DST-safe: ZoneInfo via insights/timewindow.py — single source of truth
  for week math, shared with routes/me.py and routes/admin_data.py.
- Startup catchup fail-CLOSED by default: requires explicit
  WISPRALT_INSIGHTS_CATCHUP_ENABLED=1 to fire, so a fresh deploy on a
  Wednesday doesn't burn $$ analyzing last week's stale data.
- Per-team retry: on the team pass, a transient JSONDecodeError gets one
  retry at temperature=0.0 before being logged + skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from wispralt_server.config import settings
from wispralt_server.insights.client import (
    InsightsClient,  # noqa: F401 — re-exported for `__main__` smoke
    InsightsError,
    RateLimitedError,
)
from wispralt_server.insights.prompts import (
    PERSON_SYSTEM_PROMPT,
    PERSON_USER_PROMPT_TEMPLATE,
    TEAM_SYSTEM_PROMPT,
    TEAM_USER_PROMPT_TEMPLATE,
)
from wispralt_server.insights.timewindow import (
    iso_week_epoch_bounds,
    last_full_iso_week,
)

logger = logging.getLogger(__name__)


async def run_weekly_insights(app) -> None:
    """One full Sunday-night sweep. Idempotent — re-runs for the same week
    are overwrites via the upsert.

    Concurrency: holds ``app.state.weekly_insights_lock`` for the entire run
    so an overlapping catchup or rescheduled fire never overlaps. If the
    lock is already held, this returns immediately with a warning log.
    """
    lock = getattr(app.state, "weekly_insights_lock", None)
    if lock is None:
        # Test / __main__ path — no FastAPI lifespan to install the lock.
        # Concurrency isn't possible here so just run the inner body.
        return await _run_weekly_insights_inner(app)
    if lock.locked():
        logger.warning(
            "Weekly insights skipped — another run still in progress"
        )
        return
    async with lock:
        return await _run_weekly_insights_inner(app)


async def _run_weekly_insights_inner(app) -> None:
    """Body of :func:`run_weekly_insights`. See its docstring."""
    insights_client: InsightsClient | None = getattr(
        app.state, "insights_client", None
    )
    if insights_client is None:
        logger.info(
            "Weekly insights skipped — no InsightsClient (OPENROUTER_API_KEY unset)"
        )
        return

    job_store = app.state.job_store
    db_pool = getattr(app.state, "db_pool", None)
    if db_pool is None:
        logger.warning(
            "Weekly insights skipped — db_pool is None (Supabase unavailable)"
        )
        return

    # Snapshot budget config at the top so a hot-reload mid-run doesn't change
    # the cap underneath the loop.
    BUDGET = float(settings.insights_max_30d_cost_usd)
    INPUT_WORD_CAP = int(settings.insights_input_word_cap)
    PER_PERSON_MIN_DICTATIONS = int(settings.insights_per_person_min_dictations)

    # 1. Budget gate
    spent_30d = job_store.rolling_insights_cost_usd(days=30)
    if spent_30d > BUDGET:
        logger.warning(
            "Weekly insights skipped — rolling 30d spend $%.2f > $%.2f cap",
            spent_30d,
            BUDGET,
        )
        return

    # 2. Target week — last full ISO week. Shared math via timewindow.py so
    #    the routes/me.py + routes/admin_data.py pages always agree with the
    #    cron about which week was just summarized.
    iso_year, iso_week = last_full_iso_week(settings.insights_timezone)
    week_start_epoch, week_end_epoch = iso_week_epoch_bounds(
        iso_year, iso_week, settings.insights_timezone,
    )

    # 3. Per-person pass
    users = await db_pool.fetch(
        "SELECT id, label, display_name, role FROM wispralt.users"
        " WHERE revoked_at IS NULL"
    )

    person_digests: list[tuple[int, str, dict]] = []  # (api_key_id, name, digest)
    # Snapshot baseline BEFORE the loop. Per-person upserts persist, so a fresh
    # rolling_insights_cost_usd(30) call mid-loop would include the rows we
    # just wrote — double-counting prior iterations. Use snapshot+accumulated.
    baseline_30d_cost = job_store.rolling_insights_cost_usd(30)
    accumulated_run_cost = 0.0
    total_cost = 0.0  # accumulated across BOTH per-person and team passes

    # Per-failure-cause counters (Monday-morning operator signal).
    person_failures: dict[str, int] = {
        "rate_limit": 0,
        "openrouter_error": 0,
        "json_decode": 0,
    }

    for user in users:
        # Mid-loop budget re-check using snapshot+accumulated, NOT fresh 30d read.
        if baseline_30d_cost + accumulated_run_cost >= BUDGET:
            logger.warning(
                "Per-user budget cap hit mid-run at user_id=%s; stopping cron",
                user["id"],
            )
            break

        # Skip if this person already has a row for this ISO week — re-run
        # idempotency (don't double-charge OpenRouter on Sunday-night restart).
        if (
            job_store.get_weekly_insight_person(user["id"], iso_year, iso_week)
            is not None
        ):
            logger.info(
                "Person insight already exists for user_id=%s W%02d — skipping",
                user["id"],
                iso_week,
            )
            continue

        transcripts = await asyncio.to_thread(
            job_store.transcripts_in_range,
            user["id"],
            since_epoch=week_start_epoch,
            until_epoch=week_end_epoch,
        )
        if not transcripts:
            continue

        # Word-count gate: >=N dictations OR >=1 meeting
        dictations = [t for t in transcripts if t["kind"] == "dictation"]
        meetings = [t for t in transcripts if t["kind"] == "meeting"]
        if len(dictations) < PER_PERSON_MIN_DICTATIONS and len(meetings) < 1:
            continue

        # Build prompt, capped to INPUT_WORD_CAP
        blob, total_words = _assemble_blob(transcripts, cap=INPUT_WORD_CAP)
        user_prompt = PERSON_USER_PROMPT_TEMPLATE.format(
            display_name=user["display_name"] or user["label"],
            api_key_id=user["id"],
            iso_year=iso_year,
            iso_week=iso_week,
            total_words=total_words,
            dictation_count=len(dictations),
            meeting_count=len(meetings),
            input_word_cap=INPUT_WORD_CAP,
            transcript_blob=blob,
        )

        try:
            resp = await insights_client.analyze(
                system_prompt=PERSON_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=2000,
            )
        except RateLimitedError:
            # 429: abort entire run — burning through every employee just to
            # hit the same rate-limit ten times in a row produces zero
            # insights with worse signal than retrying next week.
            person_failures["rate_limit"] += 1
            logger.warning(
                "OpenRouter 429 — aborting cron run; will retry next schedule"
            )
            return
        except InsightsError as exc:
            person_failures["openrouter_error"] += 1
            logger.warning(
                "Person insights failed for user_id=%s: %s", user["id"], exc
            )
            continue

        try:
            insight = json.loads(resp.content)
        except json.JSONDecodeError:
            person_failures["json_decode"] += 1
            logger.warning(
                "Person insights returned non-JSON for user_id=%s; first 200 chars: %r",
                user["id"],
                resp.content[:200],
            )
            continue

        # Citation guard before persist — drop any action_items / quotable_line
        # whose text does NOT appear in the input transcripts.
        insight = _scrub_hallucinations(insight, transcript_blob=blob)

        job_store.upsert_weekly_insight(
            iso_year=iso_year,
            iso_week=iso_week,
            scope="person",
            api_key_id=user["id"],
            insight=insight,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=resp.cost_usd,
            model=insights_client.model,
        )
        person_digests.append(
            (user["id"], user["display_name"] or user["label"], insight)
        )
        accumulated_run_cost += resp.cost_usd
        total_cost += resp.cost_usd

    # 4. Team pass — only if at least 2 people had insights
    if len(person_digests) >= 2:
        digests_jsonl = "\n".join(
            json.dumps(
                {"employee": dn, "api_key_id": kid, "insight": d}
            )
            for kid, dn, d in person_digests
        )
        user_prompt = TEAM_USER_PROMPT_TEMPLATE.format(
            iso_year=iso_year,
            iso_week=iso_week,
            digests_jsonl=digests_jsonl,
        )
        team_resp = None
        try:
            team_resp = await insights_client.analyze(
                system_prompt=TEAM_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=2500,
            )
        except InsightsError as exc:
            logger.warning("Team insights failed: %s", exc)

        if team_resp is not None:
            try:
                team_insight = json.loads(team_resp.content)
            except json.JSONDecodeError:
                logger.warning(
                    "Team insights returned non-JSON; first 200 chars: %r —"
                    " retrying once at temperature=0.0",
                    team_resp.content[:200],
                )
                team_insight = None
                try:
                    team_resp = await insights_client.analyze(
                        system_prompt=TEAM_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        max_tokens=2500,
                        temperature=0.0,
                    )
                    team_insight = json.loads(team_resp.content)
                except InsightsError as exc:
                    logger.warning(
                        "Team insights retry failed at temperature=0.0: %s", exc
                    )
                except json.JSONDecodeError:
                    logger.warning(
                        "Team insights still non-JSON after retry; first 200 chars: %r",
                        team_resp.content[:200] if team_resp else "",
                    )
            if team_insight is not None:
                job_store.upsert_weekly_insight(
                    iso_year=iso_year,
                    iso_week=iso_week,
                    scope="team",
                    api_key_id=None,
                    insight=team_insight,
                    input_tokens=team_resp.input_tokens,
                    output_tokens=team_resp.output_tokens,
                    cost_usd=team_resp.cost_usd,
                    model=insights_client.model,
                )
                total_cost += team_resp.cost_usd

    logger.info(
        "Weekly insights complete — %d people, total cost $%.4f"
        " (30d rolling: $%.2f) failures=%s",
        len(person_digests),
        total_cost,
        job_store.rolling_insights_cost_usd(30),
        person_failures,
    )


def _assemble_blob(transcripts: list[dict], *, cap: int) -> tuple[str, int]:
    """Newest-first concatenation, truncated to *cap* words.

    Returns ``(blob, total_word_count_before_truncation)``.
    """
    chunks: list[str] = []
    total = 0
    used = 0
    for t in transcripts:
        text = t["text"] or ""
        n = len(text.split())
        total += n
        if used + n > cap:
            # Truncate this one to fit
            remaining = cap - used
            if remaining > 0:
                chunks.append(" ".join(text.split()[:remaining]))
                used += remaining
            break
        chunks.append(text)
        used += n
    return ("\n---\n".join(chunks), total)


def _scrub_hallucinations(insight: dict, *, transcript_blob: str) -> dict:
    """Two-tier hallucination guard:

    1. ``quotable_line``: EXACT full-string substring match (case-insensitive).
       The UI displays this verbatim with quotes, so any hallucination is
       immediate harm. Strict.
    2. ``action_items`` / ``decisions`` / ``blockers``: 8-char sliding-window
       match. Looser because these get paraphrased a little; trade-off
       documented:
       - false negatives (legit short items dropped) — annoying but safe.
       - false positives (invented items kept because they share 8 chars
         with the blob) — exists; mitigated by displaying "AI-generated,
         may be inaccurate" disclaimer in the UI.

    Keeps untouched: ``digest`` (paraphrase is intentional), ``topics``,
    ``projects``, ``filler_word_count`` (aggregations).

    Sets ``insight["_hallucination_scrubbed"] = True`` sentinel so downstream
    consumers can assert this pass actually ran.
    """
    blob_lower = transcript_blob.lower()

    def loose_supported(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        # Slide an 8-char window; any hit counts as "grounded enough".
        return any(t[i : i + 8] in blob_lower for i in range(0, max(1, len(t) - 7)))

    insight["action_items"] = [
        a
        for a in insight.get("action_items", [])
        if loose_supported(a.get("text", "") if isinstance(a, dict) else str(a))
    ]
    insight["decisions"] = [
        d for d in insight.get("decisions", []) if loose_supported(d)
    ]
    insight["blockers"] = [
        b for b in insight.get("blockers", []) if loose_supported(b)
    ]

    # quotable_line — strict exact-substring match (verbatim UI display)
    q = (insight.get("quotable_line") or "").lower()
    if q and q not in blob_lower:
        insight["quotable_line"] = ""

    insight["_hallucination_scrubbed"] = True
    return insight


def _seconds_until_next_fire(
    tz_name: str,
    *,
    iso_weekday: int = 7,
    hour: int = 23,
) -> float:
    """Seconds until the next ``hour:00`` on ``iso_weekday`` in the named timezone.

    ``iso_weekday`` follows ISO: Mon=1...Sun=7 (default 7 = Sunday).
    ``hour`` is 0..23 local-time wall clock (default 23 = 23:00).

    Returns ``>= 60.0`` always — safety lower-bound against negative drift
    from clock skew. Returns ``<= 7 * 86400`` always — next-occurrence window.
    """
    if not 1 <= iso_weekday <= 7:
        raise ValueError(f"iso_weekday must be 1..7, got {iso_weekday}")
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0..23, got {hour}")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    days_ahead = (iso_weekday - now.isoweekday()) % 7
    if days_ahead == 0 and now.hour >= hour:
        days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    )
    delta = (target - now).total_seconds()
    return max(60.0, delta)


# Backward-compat alias retained for one release so external smoke scripts /
# the implementation-reviewer rerun don't break. New code uses the parameterized
# form above.
def _seconds_until_sunday_23_local(tz_name: str) -> float:
    return _seconds_until_next_fire(tz_name, iso_weekday=7, hour=23)


async def _maybe_catchup(app) -> None:
    """If the most-recent completed ISO week has no row in weekly_insights for
    any user AND it ended within the last 72 h, run insights now.

    Covers Sunday-22:59 reboots and any 1-2-day server outage that crossed
    Sunday 23:00. Fail-closed by default:

    - ``settings.insights_catchup_enabled`` defaults to FALSE — explicit opt-in
      after Task 0 spike verification. Operator flips to TRUE in ``.env`` ONCE,
      after confirming OpenRouter rates + model.
    - Check ANY weekly_insights row for the target week — not just team row
      (team only writes if >=2 people, so single-employee deployments would
      re-fire every startup).
    """
    if not settings.insights_catchup_enabled:
        logger.info(
            "Startup catchup disabled — set WISPRALT_INSIGHTS_CATCHUP_ENABLED=1"
            " to enable"
        )
        return

    tz = ZoneInfo(settings.insights_timezone)
    now = datetime.now(tz)
    days_since_monday = (now.isoweekday() - 1) % 7
    this_monday = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    if (now - this_monday).total_seconds() > 72 * 3600:
        return
    iso_year, iso_week = last_full_iso_week(settings.insights_timezone)

    job_store = app.state.job_store
    # Check for ANY row this week — covers single-employee teams (no team row).
    if job_store.has_any_insight_for_week(iso_year, iso_week):
        return  # already analyzed for this week
    logger.info(
        "Catchup: running missed weekly insights for ISO %d-W%02d",
        iso_year,
        iso_week,
    )
    await run_weekly_insights(app)


if __name__ == "__main__":
    # Manual smoke gate (d) — run one cron pass without booting FastAPI/uvicorn.
    # `python -m wispralt_server.insights.cron --manual`
    import argparse
    from pathlib import Path
    from types import SimpleNamespace

    from wispralt_server import db as db_module
    from wispralt_server.jobs.store import JobStore

    parser = argparse.ArgumentParser(
        description="Manual weekly-insights cron runner (smoke gate d)."
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Run one cron pass immediately",
    )
    args = parser.parse_args()
    if not args.manual:
        parser.error("--manual required (no-op without it)")

    async def _main() -> None:
        # Verify required config — fail loud, not with an obscure AttributeError.
        if not settings.openrouter_api_key:
            parser.error(
                "OPENROUTER_API_KEY not set in env — cannot run insights cron"
            )
        if not settings.supabase_database_url:
            parser.error(
                "SUPABASE_DATABASE_URL not set — cannot fetch user list"
            )

        # Reuse the existing pool helper instead of asyncpg.create_pool directly
        # so the manual path matches the production lifespan at main.py.
        pool = await db_module.recreate_pool()

        app = SimpleNamespace(
            state=SimpleNamespace(
                job_store=JobStore(Path(settings.job_db_path)),
                insights_client=InsightsClient(
                    api_key=settings.openrouter_api_key,  # plain str, NOT SecretStr
                    model=settings.insights_model,
                    timeout_s=settings.insights_timeout_s,
                ),
                db_pool=pool,
                # No lock — __main__ path is single-process and never overlaps.
            )
        )
        try:
            await run_weekly_insights(app)
        finally:
            await app.state.insights_client.aclose()
            await db_module.close_pool()

    asyncio.run(_main())
