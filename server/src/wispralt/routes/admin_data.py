"""routes/admin_data.py — Data & Insights, team records and AI settings (admin only).

* ``GET /admin/data`` — measured stats (team, or ``?user_id=N``), the separate AI panel,
  the per-person table and the runs panel.
* ``GET /admin/records`` / ``/admin/records/{kind}/{id}`` — team transcripts (LD 15).
* ``GET /admin/data/runs/current`` — the runs list partial (HTMX polls it every 2 s while a
  run is going; it never rotates the CSRF cookie).
* ``POST /admin/data/runs`` — ``weeks`` (1..12) + ``force``: without ``confirm`` it returns
  the cost estimate and a confirm button; with ``confirm=1`` it starts the run(s) in the
  background (``insights.cron.run_weeks``; 1 week = ``manual``, more = ``backfill``).
* ``GET/POST /admin/settings`` — AI enablement (needs the disclosure ack), model, 30-day
  cap, timezone. The OpenRouter key is shown as configured or not, never accepted here.

Auth: ``require_admin`` on every route; mutations take ``require_admin_csrf``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import csrf_token_for, require_admin, require_admin_csrf, set_csrf_cookie
from ..config import settings
from ..insights import cron, measured, sources
from ..insights import settings as insights_settings
from ..insights.timewindow import (
    epoch_for_range,
    iso_week_epoch_bounds,
    last_full_iso_week,
    normalize_range,
)
from ..web import records
from ..web.htmx import is_htmx
from ..web.templates_env import templates

if TYPE_CHECKING:
    from ..accounts.store import AccountStore, User
    from ..jobs.store import JobStore

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin", tags=["admin-data"])


# ── route ────────────────────────────────────────────────────────────────────


@router.get("/data", response_class=HTMLResponse)
async def admin_data(
    request: Request,
    user: User = Depends(require_admin),
    range: str = "7d",
    user_id: int | None = None,
):
    """Admin Data tab — team overview, leaderboards, drill-down by ?user_id."""
    range = normalize_range(range, "7d")
    cfg = await insights_settings.load_for(request.app)
    tz = cfg.timezone
    iso_year, iso_week = last_full_iso_week(tz)
    job_store: JobStore = request.app.state.job_store
    accounts: AccountStore = request.app.state.accounts
    usage = request.app.state.usage
    now = time.time()
    since = epoch_for_range(range, tz)

    bounds = iso_week_epoch_bounds(iso_year, iso_week, tz)
    m = await asyncio.to_thread(
        lambda: measured.compute(
            job_store, usage, user_id=user_id, since=since, until=now, now=now, tz_name=tz
        )
    )
    leaderboard: list[dict[str, Any]] = []
    row = job_store.weekly_insight(user_id, iso_year, iso_week)
    ai = await asyncio.to_thread(
        lambda: sources.build_view(
            job_store, row, viewer_id=user.id, is_admin=True, scope_user_id=user_id,
            bounds=bounds, iso_week=(iso_year, iso_week), link_base="/admin/records",
        )
    )
    if user_id is not None:
        target_user = await asyncio.to_thread(accounts.profile, user_id)
        users = [target_user] if target_user is not None else []
    else:
        target_user = None
        users = await asyncio.to_thread(accounts.list_people, active=True)
        # Per-person table: one grouped stats query + one pass over dictation text (no N+1).
        activity = await asyncio.to_thread(job_store.stats_by_user, since)
        fillers = await asyncio.to_thread(measured.fillers_by_user, job_store, since, now)
        for u in users:
            a = activity.get(u.id, {})
            leaderboard.append({
                "id": u.id,
                "name": u.display_name or u.label,
                "dictations": a.get("dictation_count", 0),
                "files": a.get("file_count", 0),
                "words": a.get("total_words", 0),
                "fillers": fillers.get(u.id, 0),
                "time_saved_hours": measured.time_saved_hours(int(a.get("total_words", 0))),
            })
        leaderboard.sort(key=lambda r: (-r["words"], r["name"]))

    ctx = {
        "request": request,
        "user": user,
        "range": range,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "ai": ai,
        "ai_enabled": cfg.enabled,
        "ai_team": user_id is None,
        "viewer_is_admin": True,
        "users": users,
        "user_id": user_id,
        "target_user": target_user,
        "m": m,
        "link_base": "/admin/records",
        "link_params": {"user_id": user_id} if user_id is not None else {},
        "leaderboard": leaderboard,
    }

    if is_htmx(request):
        # HTMX partial (range tabs): never mint or rotate the CSRF cookie, or the runs
        # form already on the page would carry a stale token (silent 403).
        return templates.TemplateResponse(request, "_admin_data_body.html.j2", ctx)

    # Full page: the CSRF token the runs form carries, plus the runs list.
    csrf = csrf_token_for(request)
    ctx.update(await _runs_ctx(request, cfg))
    ctx["csrf_token"] = csrf
    response = templates.TemplateResponse(request, "data.html.j2", ctx)
    set_csrf_cookie(response, csrf, request)
    return response


# ── /admin/records — team transcripts (LD 15: admins may read them) ─────────


@router.get("/records", response_class=HTMLResponse)
async def admin_records(
    request: Request,
    _admin: User = Depends(require_admin),
    q: str | None = None,
    user_id: int | None = None,
    kind: str | None = None,
    range: str = "30d",
    since: float | None = None,
    until: float | None = None,
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    """Team search (``search_history_team``) or the newest-first team list, with owners."""
    if kind not in ("dictation", "file"):
        kind = None
    range = normalize_range(range, "30d")
    tz = await insights_settings.timezone_for(request.app)
    lo, hi = records.window(range, since, until, tz)
    query = records.wants_search(q)
    job_store: JobStore = request.app.state.job_store
    page = settings.history_page_size
    if query is not None:
        hits = await asyncio.to_thread(
            job_store.search_history_team, query, user_id=user_id,
            since=lo, until=hi, kind=kind, limit=page + 1, offset=offset,
        )
        rows = [records.hit_row(h, tz) for h in hits]
    else:
        raw = await asyncio.to_thread(
            lambda: job_store.list_records_team(
                user_id=user_id, since=lo, until=hi, kind=kind, limit=page + 1, offset=offset
            )
        )
        rows = [records.decorate(r, tz) for r in raw]
    people = await asyncio.to_thread(request.app.state.accounts.list_people)
    ctx = {
        "request": request,
        "rows": rows[:page],
        "next_offset": offset + page if len(rows) > page else None,
        "q": q or "",
        "searching": query is not None,
        "user_id": user_id,
        "kind": kind,
        "range": range,
        "since": since,
        "until": until,
        "people": people,
        "load_more_url": "/admin/records",
        "filter_vals": {
            k: v
            for k, v in {
                "q": q, "user_id": user_id, "kind": kind, "range": range,
                "since": since, "until": until,
            }.items()
            if v not in (None, "")
        },
    }
    if offset:
        return templates.TemplateResponse(request, "_records_page.html.j2", ctx)
    if is_htmx(request):
        return templates.TemplateResponse(request, "_records_body.html.j2", ctx)
    return templates.TemplateResponse(request, "records.html.j2", ctx)


@router.get("/records/{kind}/{row_id}", response_class=HTMLResponse)
async def admin_record(
    request: Request, kind: str, row_id: str, admin: User = Depends(require_admin)
) -> HTMLResponse:
    job_store: JobStore = request.app.state.job_store
    row = await asyncio.to_thread(
        lambda: job_store.get_record_scoped(kind, row_id, viewer_id=admin.id, is_admin=True)
    )
    if row is None:
        raise HTTPException(status_code=404)
    tz = await insights_settings.timezone_for(request.app)
    row = {**records.decorate(row, tz), "kind": kind, "row_id": row_id}
    return templates.TemplateResponse(
        request, "record_detail.html.j2", {"request": request, "row": row, "admin_view": True}
    )


# ── insight runs: estimate → confirm → background run; progress partial ─────


async def _runs_ctx(request: Request, cfg: insights_settings.InsightsSettings) -> dict[str, Any]:
    runs = await asyncio.to_thread(request.app.state.job_store.recent_insight_runs, 10)
    return {
        "runs": runs,
        "running": any(r["status"] == "running" for r in runs),
        "ai_enabled": cfg.enabled,
        "client_ready": getattr(request.app.state, "insights_client", None) is not None,
        "week_choices": list(range(1, cron.MAX_BACKFILL_WEEKS + 1)),
    }


@router.get("/data/runs/current", response_class=HTMLResponse)
async def runs_current(request: Request, _admin: User = Depends(require_admin)) -> HTMLResponse:
    cfg = await insights_settings.load_for(request.app)
    ctx = {"request": request, **await _runs_ctx(request, cfg)}
    return templates.TemplateResponse(request, "_runs_list.html.j2", ctx)


def _track(app: Any, coro: Any) -> asyncio.Task:
    """Start a background run with a strong reference; log (never swallow) a crash."""
    tasks: set[asyncio.Task] | None = getattr(app.state, "insights_run_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.insights_run_tasks = tasks
    task = asyncio.create_task(coro)
    tasks.add(task)

    def done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Insights run crashed: %s", type(t.exception()).__name__)

    task.add_done_callback(done)
    return task


@router.post("/data/runs", response_class=HTMLResponse)
async def runs_start(
    request: Request,
    weeks: int = Form(1),
    force: bool = Form(False),
    confirm: bool = Form(False),
    csrf_token: str = Form(""),
    admin: User = Depends(require_admin_csrf),
) -> HTMLResponse:
    """Estimate first; start only on ``confirm``. Returns the ``#run-confirm`` fragment."""
    app = request.app
    cfg = await insights_settings.load_for(app)
    weeks = max(1, min(int(weeks), cron.MAX_BACKFILL_WEEKS))
    ctx: dict[str, Any] = {"request": request, "csrf_token": csrf_token, "weeks": weeks,
                           "force": force}
    if not cfg.enabled or getattr(app.state, "insights_client", None) is None:
        ctx["error"] = (
            "AI interpretation is off. Turn it on in Settings first." if not cfg.enabled
            else "OPENROUTER_API_KEY is not configured on the server."
        )
        return templates.TemplateResponse(request, "_run_confirm.html.j2", ctx, status_code=409)
    target = cron.last_weeks(weeks, cfg.timezone)
    lock = getattr(app.state, "weekly_insights_lock", None)
    if lock is not None and lock.locked():
        ctx["error"] = "A run is already in progress. Watch it below."
        return templates.TemplateResponse(request, "_run_confirm.html.j2", ctx, status_code=409)
    if not confirm:
        ctx["estimate"] = await asyncio.to_thread(
            cron.estimate_cost, app.state.job_store, app.state.accounts, target,
            model=cfg.model, tz_name=cfg.timezone,
        )
        ctx["target"] = target
        ctx["spent_30d"] = await asyncio.to_thread(app.state.job_store.rolling_insights_cost_usd, 30)
        ctx["cap"] = cfg.cap_30d_usd
        ctx["confirm_vals"] = {"weeks": weeks, "confirm": "1", "csrf_token": csrf_token,
                               **({"force": "1"} if force else {})}
        return templates.TemplateResponse(request, "_run_confirm.html.j2", ctx)
    trigger = "manual" if weeks == 1 else "backfill"
    _track(app, cron.run_weeks(app, target, trigger=trigger, force=force))
    logger.info("Insights %s started by admin_id=%s for %d week(s)", trigger, admin.id, weeks)
    ctx["started"] = True
    return templates.TemplateResponse(request, "_run_confirm.html.j2", ctx)


# ── /admin/settings — AI enablement + disclosure, model, cap, timezone ──────


async def _settings_page(request: Request, *, error: str | None = None, saved: bool = False,
                         status_code: int = 200) -> HTMLResponse:
    cfg = await insights_settings.load_for(request.app)
    stored_tz = await asyncio.to_thread(
        request.app.state.settings_store.get, insights_settings.TIMEZONE
    )
    csrf = csrf_token_for(request)
    response = templates.TemplateResponse(
        request,
        "settings.html.j2",
        {
            "request": request,
            "cfg": cfg,
            "stored_tz": stored_tz,
            "key_configured": bool(settings.openrouter_api_key),
            "client_ready": getattr(request.app.state, "insights_client", None) is not None,
            "error": error,
            "saved": saved,
            "csrf_token": csrf,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf, request)
    return response


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, _admin: User = Depends(require_admin)) -> HTMLResponse:
    return await _settings_page(request, saved=request.query_params.get("saved") == "1")


@router.post("/settings", response_class=HTMLResponse)
async def settings_submit(
    request: Request,
    enabled: bool = Form(False),
    acknowledge: bool = Form(False),
    model: str = Form(""),
    cap_30d_usd: str = Form(""),
    timezone: str = Form(""),
    admin: User = Depends(require_admin_csrf),
) -> Any:
    model = model.strip() or settings.insights_model
    timezone = timezone.strip()
    try:
        cap = float(cap_30d_usd)
    except ValueError:
        cap = -1.0
    error = None
    if not (0 <= cap <= 1000):
        error = "The 30-day cap must be a number of dollars between 0 and 1000."
    elif len(model) > 100 or not all(c.isalnum() or c in "-_./:" for c in model):
        error = "That model name is not valid (OpenRouter ids look like x-ai/grok-4.3)."
    elif timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            error = f"Unknown timezone: {timezone!r} (use an IANA name like Europe/Berlin)."
    if error is None:
        try:
            await asyncio.to_thread(
                lambda: insights_settings.save(
                    request.app.state.settings_store, enabled=enabled, acknowledge=acknowledge,
                    model=model, cap_30d_usd=cap, timezone=timezone or None,
                )
            )
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        return await _settings_page(request, error=error, status_code=400)
    logger.info("Insights settings saved by admin_id=%s enabled=%s", admin.id, enabled)
    return RedirectResponse("/admin/settings?saved=1", status_code=303)
