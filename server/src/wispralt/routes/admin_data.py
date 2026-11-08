"""routes/admin_data.py — Admin Data tab.

Mounts at ``GET /admin/data`` on a dedicated APIRouter. Drill-down to a
single employee via the ``?user_id=N`` query parameter; without it the page
shows the team overview (leaderboards + team-scope insight).

Lives in its own module (separate from :mod:`routes.admin_ui`) to avoid
merge contention with the existing 600-line admin UI file and to keep the
Phase 2 surface area isolated.

Auth: the handler param ``user: User = Depends(require_admin)`` is the single
source of admin-role enforcement. The user list comes from ``AccountStore``.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ..auth import require_admin, set_csrf_cookie, verify_csrf
from ..config import settings
from ..insights import measured, sources
from ..insights.cron import run_weekly_insights
from ..insights.settings import ai_enabled
from ..insights.timewindow import epoch_for_range, iso_week_epoch_bounds, last_full_iso_week
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
    tz = settings.insights_timezone
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

    # Lock state drives the button's enabled/disabled paint on first render.
    # The lock is the same one `run_weekly_insights` holds for the duration of
    # a run; `locked()` is True for the exact lifetime we want the button
    # disabled. See `insights/cron.py:71-82`.
    lock = getattr(request.app.state, "weekly_insights_lock", None)
    weekly_insights_running = bool(lock is not None and lock.locked())

    ctx = {
        "request": request,
        "user": user,
        "range": range,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "ai": ai,
        "ai_enabled": await ai_enabled(request.app),
        "ai_team": user_id is None,
        "viewer_is_admin": True,
        "users": users,
        "user_id": user_id,
        "target_user": target_user,
        "m": m,
        "link_base": "/admin/records",
        "link_params": {"user_id": user_id} if user_id is not None else {},
        "leaderboard": leaderboard,
        "weekly_insights_running": weekly_insights_running,
    }

    if is_htmx(request):
        # HTMX partial branch — do NOT mint/rotate the CSRF cookie. The button
        # lives in the outer page wrapper (not in `_admin_data_body.html.j2`),
        # and rotating the cookie here would invalidate the in-page button's
        # already-rendered `hx-vals` token → silent 403 on next click.
        return templates.TemplateResponse(
            request, "_admin_data_body.html.j2", ctx,
        )

    # Full-page render — mint a fresh CSRF token + set the double-submit cookie
    # so the "Run insights now" button's `hx-vals` value matches the cookie.
    csrf = secrets.token_urlsafe(32)
    ctx["csrf_token"] = csrf
    response = templates.TemplateResponse(request, "data.html.j2", ctx)
    set_csrf_cookie(response, csrf)
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
    tz = settings.insights_timezone
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
    row = {**records.decorate(row, settings.insights_timezone), "kind": kind, "row_id": row_id}
    return templates.TemplateResponse(
        request, "record_detail.html.j2", {"request": request, "row": row, "admin_view": True}
    )


# ── manual weekly-insights trigger ───────────────────────────────────────────


def _toast_html(message: str) -> str:
    """Inline toast fragment for HTMX swap into `#admin-run-insights-toast`.

    `html.escape` keeps any future dynamic content from injecting markup.
    """
    safe = html.escape(message)
    return (
        f'<div role="status" aria-live="polite" class="admin-toast">'
        f"{safe}</div>"
    )


def _render_button(running: bool, csrf_token: str, *, oob: bool) -> str:
    """Render the run-insights button partial to a string.

    Used both for inline composition in the POST response (OOB swap variant
    with `oob=True`) and reachable as a callable for future server-side
    composition needs. The initial page render goes through Jinja's `include`.
    """
    return templates.env.get_template(
        "_admin_run_insights_button.html.j2"
    ).render(
        {
            "running": running,
            "csrf_token": csrf_token,
            "oob": oob,
        }
    )


def _make_done_callback(pending: set[asyncio.Task]):
    """Factory for `add_done_callback` that discards + logs exceptions.

    The bare `set.discard` form silently absorbs unhandled task exceptions
    (asyncio only surfaces them at process-exit). The closure form here keeps
    the strong-ref to `pending` without poking attributes onto the task.
    """

    def cb(task: asyncio.Task) -> None:
        pending.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "Manual insights task crashed", exc_info=exc,
            )

    return cb


@router.post("/data/insights/run-now", response_class=HTMLResponse)
async def admin_data_run_insights_now(
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_admin),
) -> HTMLResponse:
    """Admin-only manual trigger for `run_weekly_insights` against the current
    in-progress ISO week. Fire-and-forget — returns immediately with a toast
    + OOB button-swap fragment for HTMX.

    The button's CSRF cookie is minted on the full-page render of
    `GET /admin/data` (NOT on the HTMX partial branch — see `admin_data`).
    Per-week double-billing safety lives in `cron.py:167-176`'s per-user
    idempotency skip, NOT in `lock.locked()` — the pre-check here is purely
    UX.
    """
    # CSRF — return 403 WITH a toast body so HTMX's `hx-target-403` swaps it.
    if not verify_csrf(request, csrf_token):
        return HTMLResponse(
            _toast_html(
                "Session expired. Refresh the page and try again."
            ),
            status_code=403,
            media_type="text/html",
        )

    app = request.app

    # Pre-check 1: insights_client must be available. Without it the cron
    # silently early-exits with logger.info — the admin would see a "started"
    # toast and nothing would happen. Surface it explicitly.
    if getattr(app.state, "insights_client", None) is None:
        return HTMLResponse(
            _toast_html(
                "Insights unavailable — OPENROUTER_API_KEY not configured."
            ),
            status_code=200,
            media_type="text/html",
        )

    # Pre-check 2: 30-day budget. Mirrors `cron.py:108-120` exactly so the
    # admin sees the same dollar amounts the cron would have logged.
    job_store: JobStore = app.state.job_store
    spent_30d = job_store.rolling_insights_cost_usd(days=30)
    budget = float(settings.insights_max_30d_cost_usd)
    if spent_30d > budget:
        return HTMLResponse(
            _toast_html(
                f"Budget exceeded (${spent_30d:.2f} / ${budget:.2f} cap). "
                "Run skipped."
            ),
            status_code=200,
            media_type="text/html",
        )

    # Pre-check 3: in-flight. Purely UX — TOCTOU is harmless because
    # `run_weekly_insights` re-checks the lock and the per-user idempotency
    # skip prevents any double-billing on overlapping spawns.
    lock = app.state.weekly_insights_lock
    if lock.locked():
        return HTMLResponse(
            _toast_html(
                "A run is already in progress. Refresh in ~1 minute."
            )
            + _render_button(running=True, csrf_token=csrf_token, oob=True),
            status_code=200,
            media_type="text/html",
        )

    # Target the LAST FULL ISO week — same week the page header displays via
    # last_full_iso_week(). Targeting the in-progress week would produce rows
    # that never appear on /admin/data because the page is keyed on the last
    # full week. Sunday's natural cron will UPSERT-replace these rows when
    # the next full week becomes available; no orphan-row risk.
    iso_year, iso_week = last_full_iso_week(settings.insights_timezone)

    # Spawn fire-and-forget with strong-ref. force=True bypasses the per-user
    # idempotency skip in run_weekly_insights so re-clicking the button always
    # produces fresh insights (and re-triggers the team-aggregate pass even
    # when person rows already exist). Lock + 30d budget guard still apply.
    pending: set[asyncio.Task] | None = getattr(
        app.state, "weekly_insights_manual_tasks", None,
    )
    if pending is None:
        pending = set()
        app.state.weekly_insights_manual_tasks = pending
    task = asyncio.create_task(
        run_weekly_insights(
            app, iso_override=(iso_year, iso_week), force=True,
        ),
    )
    pending.add(task)
    task.add_done_callback(_make_done_callback(pending))

    logger.info(
        "Manual insights run started by admin_id=%s for W%02d (user=%s)",
        user.id,
        iso_week,
        user.label,
    )

    body = (
        _toast_html(
            f"Insights run started for W{iso_week:02d}. "
            "Refresh page in ~1 minute to see results."
        )
        + _render_button(running=True, csrf_token=csrf_token, oob=True)
    )
    return HTMLResponse(body, status_code=200, media_type="text/html")
