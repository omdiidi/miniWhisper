"""Weekly AI insights: runs, the hourly scheduler, backfill and the cost estimate.

AI is opt-in (``insights/settings.py``): nothing here calls OpenRouter unless an admin has
enabled it after the disclosure, and an InsightsClient exists (``OPENROUTER_API_KEY``).

* ``scheduler_loop`` asks ``is_due`` once an hour: the last full ISO week (in the insights
  timezone) has no ``done`` scheduled run, and no scheduled attempt started in the last
  6 hours. Missed weeks self-heal and sleep/DST cannot skip a run. Older weeks are only
  ever run explicitly (backfill).
* ``run_weeks`` runs one or more weeks sequentially under ``app.state.weekly_insights_lock``;
  ``run_weekly_insights`` is the one-week case. Every run writes an ``insight_runs`` row
  (progress per person; ``failures`` = ``{user_id, error_class}``, never text) and ends
  ``done``, ``failed`` (every person failed, some person failed, a team error, or a crash),
  ``aborted`` (``budget``, ``rate_limit`` or ``cancelled``) or ``skipped`` (AI off, no
  client).
* One run at a time across processes: ``JobStore.start_insight_run`` refuses while another
  row is ``running`` (check + insert in one ``BEGIN IMMEDIATE``; a row older than
  ``RUN_LOCK_STALE_S`` is a crash and is ignored). So the server and a CLI run never spend
  the same remaining budget; the in-process lock only saves the DB round trip.
* The 30-day cost cap is checked before EVERY OpenRouter call, not once per run, and the
  check reserves that call's worst case (``call_reserve_usd``: the prompt's tokens plus a
  full ``max_tokens`` reply at the rate card), so a call that could cross the cap is never
  made.
* A rerun without ``force`` skips people who already have a row, but the team pass still
  reads every person insight of the week (saved or new). The team pass runs when there is
  no team row yet, when anyone new was analysed, or with ``force``, and only when no
  person failed: a team insight missing someone would be saved as complete. A person
  failure ends the run ``failed`` (``some_people_failed``), so the scheduler retries it and
  the retry pays only for the people still without a row. A team call that fails
  (OpenRouter error, or unusable twice) ends the run ``failed`` (``team_openrouter_error`` /
  ``team_unusable``), so the scheduler retries it.
* Each person's records go into the prompt behind their refs (``sources.assemble``); the
  reply is parsed and cleaned by ``sources.validate_person``; the team pass may cite only
  refs those person insights cite. Logs carry ids, classes and lengths, never text.
* ``estimate_cost`` = words × 1.35 / 1000 × the model's input rate, shown before a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from wispralt.config import settings
from wispralt.insights import settings as insights_settings
from wispralt.insights import sources
from wispralt.insights.client import (
    InsightsClient,
    InsightsError,
    RateLimitedError,
    input_rate_per_1k,
    worst_case_cost_usd,
)
from wispralt.insights.prompts import (
    PERSON_SYSTEM_PROMPT,
    PERSON_USER_PROMPT_TEMPLATE,
    TEAM_SYSTEM_PROMPT,
    TEAM_USER_PROMPT_TEMPLATE,
)
from wispralt.insights.timewindow import iso_week_epoch_bounds, last_full_iso_week

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL_S = 3600.0
SCHEDULER_FIRST_CHECK_S = 60.0
RETRY_AFTER_S = 6 * 3600.0
RUN_LOCK_STALE_S = 2 * 3600.0  # a 'running' row older than this is a crashed run
TOKENS_PER_WORD = 1.35
MAX_BACKFILL_WEEKS = 12
PERSON_MAX_TOKENS = 2000
TEAM_MAX_TOKENS = 2500
CHARS_PER_TOKEN_FLOOR = 3.0  # generous: real prompts average ~4 chars per token

Week = tuple[int, int]


@dataclass
class RunOutcome:
    """What one run did. ``failures`` holds ``{user_id, error_class}`` only (never text)."""

    people_total: int = 0
    people_done: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    aborted: str | None = None  # 'rate_limit' | 'budget'
    team_error: str | None = None  # 'team_openrouter_error' | 'team_unusable'


class _RunBusy(Exception):  # noqa: N818 — internal signal, never escapes run_weeks
    """Another run (this process or another) holds the DB run lock."""


def _qualifies(dictations: int, files: int) -> bool:
    """Activity gate: enough dictations, or at least one file transcription."""
    return dictations >= settings.insights_per_person_min_dictations or files >= 1


# ── estimate ─────────────────────────────────────────────────────────────────


def call_reserve_usd(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> float:
    """Worst-case cost of one call, reserved against the cap before it is made: input
    tokens counted generously (the larger of 1.35/word and 1 per 3 chars) plus a full
    ``max_tokens`` reply, at the model's rate card."""
    text = system_prompt + "\n" + user_prompt
    input_tokens = max(len(text.split()) * TOKENS_PER_WORD, len(text) / CHARS_PER_TOKEN_FLOOR)
    return worst_case_cost_usd(model, int(input_tokens) + 1, max_tokens)


def estimate_cost(job_store, accounts, weeks: list[Week], *, model: str, tz_name: str) -> dict[str, float]:  # noqa: ANN001
    """Input-side estimate for running *weeks*: words actually sent (per person, after the
    activity gate and the word cap) × 1.35 tokens/word × the model's input rate."""
    active = {u.id for u in accounts.list_people(active=True)}
    words = 0
    for week in weeks:
        since, until = iso_week_epoch_bounds(*week, tz_name)
        for uid, w in job_store.words_by_user(since, until).items():
            if uid in active and _qualifies(w["dictations"], w["files"]):
                words += min(w["words"], settings.insights_input_word_cap)
    usd = words * TOKENS_PER_WORD / 1000.0 * input_rate_per_1k(model)
    return {"words": float(words), "usd": round(usd, 4)}


def last_weeks(n: int, tz_name: str, now: float | None = None) -> list[Week]:
    """The last *n* full ISO weeks, oldest first."""
    newest = date.fromisocalendar(*last_full_iso_week(tz_name, now), 1)
    return [(newest - timedelta(weeks=k)).isocalendar()[:2] for k in range(n - 1, -1, -1)]


# ── scheduling ───────────────────────────────────────────────────────────────


async def is_due(app, now: float | None = None) -> bool:  # noqa: ANN001
    """True when AI is on, a client exists and the last full week needs its scheduled run."""
    cfg = await insights_settings.load_for(app)
    if not cfg.enabled or getattr(app.state, "insights_client", None) is None:
        return False
    now = time.time() if now is None else now
    week = last_full_iso_week(cfg.timezone, now)
    state = await asyncio.to_thread(app.state.job_store.scheduled_run_state, *week)
    if state["done"]:
        return False
    last = state["last_started"]
    return last is None or now - last >= RETRY_AFTER_S


async def scheduler_loop(app, *, interval_s: float = SCHEDULER_INTERVAL_S) -> None:  # noqa: ANN001
    """Hourly due-check (first check one minute after startup). Never dies on an error."""
    await asyncio.sleep(SCHEDULER_FIRST_CHECK_S)
    while not getattr(app.state, "shutting_down", False):
        try:
            if await is_due(app):
                await run_weekly_insights(app, trigger="scheduled")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — daemon loop: log and try again next hour
            logger.exception("Insights scheduler tick failed")
        await asyncio.sleep(interval_s)


# ── runs ─────────────────────────────────────────────────────────────────────


async def run_weekly_insights(
    app, week: Week | None = None, *, trigger: str = "scheduled", force: bool = False,  # noqa: ANN001
    now: float | None = None,
) -> RunOutcome | None:
    """Run one week (default: the last full week). None if another run holds the lock."""
    results = await run_weeks(app, [week] if week else None, trigger=trigger, force=force, now=now)
    return None if results is None else results[0]


async def run_weeks(
    app, weeks: list[Week] | None, *, trigger: str, force: bool = False,  # noqa: ANN001
    now: float | None = None,
) -> list[RunOutcome | None] | None:
    """Run *weeks* one after another under the insights lock; None if it is held (here
    or, through the DB run lock, by another process) before the first week starts.

    ``force`` re-generates people who already have a row (rerun); otherwise a week is
    restart-safe (people with a row are skipped, so nobody is paid for twice).
    """
    lock = getattr(app.state, "weekly_insights_lock", None)
    if lock is not None and lock.locked():
        logger.warning("Insights run skipped: another run holds the lock")
        return None
    async with lock if lock is not None else contextlib.nullcontext():
        cfg = await insights_settings.load_for(app)
        weeks = weeks or [last_full_iso_week(cfg.timezone, now)]
        results: list[RunOutcome | None] = []
        for week in weeks:
            try:
                results.append(await _run_one(app, week, cfg, trigger=trigger, force=force))
            except _RunBusy:
                logger.warning("Insights run skipped: another process is running insights")
                return results or None
        return results


async def _run_one(app, week: Week, cfg, *, trigger: str, force: bool) -> RunOutcome | None:  # noqa: ANN001
    job_store = app.state.job_store
    client: InsightsClient | None = getattr(app.state, "insights_client", None)
    estimate = await asyncio.to_thread(
        estimate_cost, job_store, app.state.accounts, [week], model=cfg.model, tz_name=cfg.timezone
    )
    run_id = await asyncio.to_thread(
        lambda: job_store.start_insight_run(
            trigger=trigger, iso_year=week[0], iso_week=week[1],
            estimated_usd=estimate["usd"], now=time.time(), stale_after_s=RUN_LOCK_STALE_S,
        )
    )
    if run_id is None:
        raise _RunBusy

    async def finish(status: str, error_class: str | None) -> None:
        await asyncio.to_thread(
            lambda: job_store.finish_insight_run(
                run_id, status=status, error_class=error_class, now=time.time()
            )
        )

    if not cfg.enabled or client is None:
        await finish("skipped", "ai_disabled" if not cfg.enabled else "no_client")
        return None

    async def progress(outcome: RunOutcome) -> None:
        await asyncio.to_thread(
            lambda: job_store.update_insight_run(
                run_id, people_total=outcome.people_total, people_done=outcome.people_done,
                failures=outcome.failures, cost_usd=outcome.cost_usd,
            )
        )

    try:
        outcome = await _run_weekly_insights_inner(app, week, cfg, force=force, progress=progress)
    except asyncio.CancelledError:
        # Shutdown: close the row so it stops holding the DB run lock.
        await finish("aborted", "cancelled")
        raise
    except Exception as exc:
        await finish("failed", type(exc).__name__)
        raise
    await progress(outcome)
    if outcome.aborted:
        await finish("aborted", outcome.aborted)
    elif outcome.people_total and not outcome.people_done:
        await finish("failed", "all_people_failed")
    elif outcome.failures:
        await finish("failed", "some_people_failed")  # no team row: the scheduler retries
    elif outcome.team_error:
        await finish("failed", outcome.team_error)  # not done: the scheduler retries it
    else:
        await finish("done", None)
    return outcome


async def _run_weekly_insights_inner(
    app, week: Week, cfg, *, force: bool = False, progress=None,  # noqa: ANN001
) -> RunOutcome:
    """One week: a person pass then a team pass. See the module docstring."""
    client: InsightsClient = app.state.insights_client
    job_store = app.state.job_store
    accounts = app.state.accounts
    outcome = RunOutcome()
    iso_year, iso_week = week
    week_start, week_end = iso_week_epoch_bounds(iso_year, iso_week, cfg.timezone)
    word_cap = int(settings.insights_input_word_cap)
    baseline = await asyncio.to_thread(job_store.rolling_insights_cost_usd, 30)

    def over_cap(reserve_usd: float) -> bool:
        """True when spend so far plus this call's worst case would cross the cap."""
        return baseline + outcome.cost_usd + reserve_usd > cfg.cap_30d_usd

    async def report() -> None:
        if progress is not None:
            await progress(outcome)

    users = await asyncio.to_thread(accounts.list_people, active=True)
    person_insights: list[tuple[str, dict]] = []  # (name, validated insight), saved or new

    for user in users:
        name = user.display_name or user.label
        if not force:
            saved = await asyncio.to_thread(
                job_store.get_weekly_insight_person, user.id, iso_year, iso_week
            )
            if saved is not None:
                # restart-safe: never pay twice for a person, but the team pass needs it
                person_insights.append((name, saved))
                continue
        transcripts = await asyncio.to_thread(
            job_store.transcripts_in_range, user.id, since_epoch=week_start, until_epoch=week_end
        )
        dictations = sum(t["kind"] == "dictation" for t in transcripts)
        files = sum(t["kind"] == "file" for t in transcripts)
        if not _qualifies(dictations, files):
            continue
        assembled = sources.assemble(transcripts, cap=word_cap)
        user_prompt = PERSON_USER_PROMPT_TEMPLATE.format(
            display_name=name,
            iso_year=iso_year,
            iso_week=iso_week,
            total_words=assembled.total_words,
            dictation_count=dictations,
            file_count=files,
            input_word_cap=word_cap,
            transcript_blob=assembled.blob,
        )
        if over_cap(call_reserve_usd(cfg.model, PERSON_SYSTEM_PROMPT, user_prompt, PERSON_MAX_TOKENS)):
            logger.warning("Insights 30-day cap would be crossed by user_id=%s; aborting", user.id)
            outcome.aborted = "budget"
            break
        outcome.people_total += 1
        await report()

        try:
            resp = await client.analyze(
                system_prompt=PERSON_SYSTEM_PROMPT, user_prompt=user_prompt,
                max_tokens=PERSON_MAX_TOKENS, model=cfg.model,
            )
        except RateLimitedError:
            # 429: stop the whole run; every later person would hit the same limit.
            outcome.failures.append({"user_id": user.id, "error_class": "rate_limit"})
            outcome.aborted = "rate_limit"
            logger.warning("OpenRouter 429: aborting the insights run")
            return outcome
        except InsightsError as exc:
            outcome.failures.append({"user_id": user.id, "error_class": "openrouter_error"})
            logger.warning(
                "Person insights failed for user_id=%s: %s status=%s",
                user.id, type(exc).__name__, exc.status_code,
            )
            await report()
            continue
        outcome.cost_usd += resp.cost_usd  # billed even if the reply is unusable

        try:
            insight = sources.validate_person(json.loads(resp.content), assembled)
        except (json.JSONDecodeError, sources.InsightShapeError) as exc:
            error_class = "json_decode" if isinstance(exc, json.JSONDecodeError) else "schema"
            outcome.failures.append({"user_id": user.id, "error_class": error_class})
            logger.warning(
                "Person insights unusable for user_id=%s: %s (%d chars)",
                user.id, error_class, len(resp.content),
            )
            await report()
            continue

        await asyncio.to_thread(
            functools.partial(
                job_store.upsert_weekly_insight,
                iso_year=iso_year, iso_week=iso_week, user_id=user.id, insight=insight,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cost_usd=resp.cost_usd, model=cfg.model,
            )
        )
        outcome.people_done += 1
        person_insights.append((name, insight))
        await report()

    # Any person failure skips the team pass: a team insight without them would be saved
    # as the week's complete one and never revisited (the run ends failed and is retried).
    if person_insights and outcome.aborted is None and not outcome.failures:
        team_saved = await asyncio.to_thread(job_store.get_weekly_insight_team, iso_year, iso_week)
        if force or team_saved is None or outcome.people_done:
            await _team_pass(app, week, cfg, person_insights, outcome, over_cap)

    logger.info(
        "Weekly insights W%02d — %d/%d people, cost $%.4f, failures=%s, aborted=%s",
        iso_week, outcome.people_done, outcome.people_total, outcome.cost_usd,
        [f["error_class"] for f in outcome.failures], outcome.aborted,
    )
    return outcome


async def _team_pass(app, week: Week, cfg, person_insights, outcome: RunOutcome, over_cap) -> None:  # noqa: ANN001
    """Team insight over every person insight of the week; it may cite only their refs.
    Sets ``outcome.team_error`` when no usable team insight was saved (budget aside)."""
    client: InsightsClient = app.state.insights_client
    allowed = set().union(*(sources.refs_in(i) for _, i in person_insights))
    digests_jsonl = "\n".join(
        json.dumps({"person": name, "insight": i}) for name, i in person_insights
    )
    user_prompt = TEAM_USER_PROMPT_TEMPLATE.format(
        iso_year=week[0], iso_week=week[1], digests_jsonl=digests_jsonl
    )
    reserve = call_reserve_usd(cfg.model, TEAM_SYSTEM_PROMPT, user_prompt, TEAM_MAX_TOKENS)
    for temperature in (0.2, 0.0):  # one retry at 0.0 when the reply is unusable
        if over_cap(reserve):
            outcome.aborted = "budget"
            return
        try:
            resp = await client.analyze(
                system_prompt=TEAM_SYSTEM_PROMPT, user_prompt=user_prompt,
                max_tokens=TEAM_MAX_TOKENS, temperature=temperature, model=cfg.model,
            )
        except RateLimitedError:
            outcome.aborted = "rate_limit"
            logger.warning("OpenRouter 429 on the team pass: aborting the insights run")
            return
        except InsightsError as exc:
            logger.warning("Team insights failed: %s status=%s", type(exc).__name__, exc.status_code)
            outcome.team_error = "team_openrouter_error"
            return
        outcome.cost_usd += resp.cost_usd
        try:
            team = sources.validate_team(json.loads(resp.content), allowed)
        except (json.JSONDecodeError, sources.InsightShapeError):
            logger.warning(
                "Team insights unusable at temperature=%.1f (%d chars)", temperature, len(resp.content)
            )
            continue
        await asyncio.to_thread(
            functools.partial(
                app.state.job_store.upsert_weekly_insight,
                iso_year=week[0], iso_week=week[1], user_id=None, insight=team,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cost_usd=resp.cost_usd, model=cfg.model,
            )
        )
        return
    outcome.team_error = "team_unusable"


if __name__ == "__main__":
    # Operator entry point: one run now, without the server. Same rules as the portal:
    # AI must be enabled in the DB and OPENROUTER_API_KEY set: Settings reads .env from the
    # working directory, so run it from ~/.config/wispralt (the service's .env) or export it.
    #   python -m wispralt.insights.cron --manual [--week YYYY-WW] [--force]
    import argparse
    from types import SimpleNamespace

    from wispralt.accounts.store import AccountStore
    from wispralt.database import Database
    from wispralt.jobs.store import JobStore
    from wispralt.settings_store import SettingsStore

    parser = argparse.ArgumentParser(description="Run weekly AI insights once.")
    parser.add_argument("--manual", action="store_true", help="run now")
    parser.add_argument("--week", metavar="YYYY-WW", help="ISO week (default: last full week)")
    parser.add_argument("--force", action="store_true", help="regenerate existing rows")
    args = parser.parse_args()
    if not args.manual:
        parser.error("--manual required")
    target: Week | None = None
    if args.week:
        try:
            _y, _w = args.week.split("-", 1)
            target = (int(_y), int(_w))
        except ValueError:
            parser.error(f"--week must be YYYY-WW (got {args.week!r})")

    async def _main() -> None:
        if not settings.openrouter_api_key:
            parser.error(
                "OPENROUTER_API_KEY is not set: add it to ~/.config/wispralt/.env and run"
                " from that directory (or export it)"
            )
        database = Database(settings.db_path)
        database.migrate()
        app = SimpleNamespace(state=SimpleNamespace(
            job_store=JobStore(database, settings.data_dir),
            accounts=AccountStore(database),
            settings_store=SettingsStore(database),
            insights_client=InsightsClient(
                api_key=settings.openrouter_api_key, model=settings.insights_model,
                timeout_s=settings.insights_timeout_s,
            ),
        ))
        try:
            outcome = await run_weekly_insights(app, target, trigger="manual", force=args.force)
            print("skipped (AI is off, or another run holds the lock)" if outcome is None else outcome)
        finally:
            await app.state.insights_client.aclose()

    asyncio.run(_main())
