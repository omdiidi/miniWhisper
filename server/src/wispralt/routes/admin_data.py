"""routes/admin_data.py — Admin Data tab.

Mounts at ``GET /admin/data`` on a dedicated APIRouter. Drill-down to a
single employee via the ``?user_id=N`` query parameter; without it the page
shows the team overview (leaderboards + team-scope insight).

Lives in its own module (separate from :mod:`routes.admin_ui`) to avoid
merge contention with the existing 600-line admin UI file and to keep the
Phase 2 surface area isolated.

Auth: router-level ``_require_db_pool`` short-circuits to 503 when Postgres
is unavailable. The handler param ``user: User = Depends(require_admin)``
is the single source of admin-role enforcement; FastAPI runs each dep once
per request so we drop the duplicate router-level ``require_admin`` here.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..auth import require_admin, set_csrf_cookie, verify_csrf
from ..config import settings
from ..insights.cron import run_weekly_insights
from ..insights.timewindow import epoch_for_range, last_full_iso_week
from ..web.htmx import is_htmx
from ..web.templates_env import templates

if TYPE_CHECKING:
    import asyncpg

    from ..jobs.store import JobStore
    from ..users.store import User

logger = logging.getLogger(__name__)


async def _require_db_pool(request: Request) -> asyncpg.Pool:
    """Dependency: 503 if the asyncpg pool is unavailable.

    Mirrors ``admin_ui._require_db_pool`` — the Data tab queries Postgres for
    the user list, so fail loudly rather than crash on AttributeError later.
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503, detail="Admin Data unavailable: Postgres degraded."
        )
    return pool


# Router-level dep covers the DB pool short-circuit. Admin-role enforcement
# lives on the handler param (typed `user`) — keeping it there as the single
# invocation point.
router = APIRouter(
    prefix="/admin",
    tags=["admin-data"],
    dependencies=[Depends(_require_db_pool)],
)


# ── route ────────────────────────────────────────────────────────────────────


@router.get("/data", response_class=HTMLResponse)
async def admin_data(
    request: Request,
    user: User = Depends(require_admin),
    range: str = "7d",
    user_id: int | None = None,
):
    """Admin Data tab — team overview, leaderboards, drill-down by ?user_id."""
    iso_year, iso_week = last_full_iso_week(settings.insights_timezone)
    job_store: JobStore = request.app.state.job_store
    pool: asyncpg.Pool = request.app.state.db_pool
    since = epoch_for_range(range, settings.insights_timezone)

    team_insight = job_store.get_weekly_insight_team(iso_year, iso_week)

    per_user_stats: dict[int, dict[str, Any]] | None = None
    per_user_fillers: list[dict[str, Any]] = []

    if user_id is not None:
        # Drill-down to one user.
        person_insight = job_store.get_weekly_insight_person(
            user_id, iso_year, iso_week
        )
        stats = await asyncio.to_thread(
            job_store.compute_user_stats, user_id, since_epoch=since,
        )
        users = await pool.fetch(
            "SELECT id, label, display_name, role FROM wispralt.users WHERE id = $1",
            user_id,
        )
        target_user = users[0] if users else None
    else:
        person_insight = None
        target_user = None
        users = await pool.fetch(
            "SELECT id, label, display_name, role FROM wispralt.users "
            "WHERE revoked_at IS NULL ORDER BY id"
        )
        # Per-user stats for the leaderboard. Bounded by user count (small on
        # this deployment); each call is a single SQLite query through the
        # same connection so no N+1 round-trip blow-up.
        per_user_stats = {}
        for u in users:
            per_user_stats[u["id"]] = await asyncio.to_thread(
                job_store.compute_user_stats, u["id"], since_epoch=since,
            )
        # Top-tile aggregate for team view — sum the per-user stats we already
        # computed instead of leaving the tiles at 0. Time-saved is re-derived
        # from total_words (40 WPM baseline) so the rounding matches the
        # per-user tile rather than summing already-rounded fractions.
        _total_words = sum(
            s.get("total_words", 0) for s in per_user_stats.values()
        )
        stats = {
            "dictation_count": sum(
                s.get("dictation_count", 0) for s in per_user_stats.values()
            ),
            "meeting_count": sum(
                s.get("meeting_count", 0) for s in per_user_stats.values()
            ),
            "total_words": _total_words,
            "total_inference_ms": sum(
                s.get("total_inference_ms", 0) for s in per_user_stats.values()
            ),
            "time_saved_hours": round(_total_words / 40.0 / 60.0, 2),
        }

        # Filler leaderboard for team view: pull every person row for this
        # week via JobStore (no _exec leak), then resolve api_key_id →
        # display_name so the UI reads "Alice: 42 fillers" instead of an opaque
        # numeric id.
        users_by_id: dict[int, str] = {
            u["id"]: (u["display_name"] or u["label"]) for u in users
        }
        for api_key_id, n in job_store.list_filler_counts_for_week(
            iso_year, iso_week,
        ):
            per_user_fillers.append(
                {
                    "api_key_id": api_key_id,
                    "name": users_by_id.get(
                        api_key_id, f"#{api_key_id} (unknown)"
                    ),
                    "filler_word_count": n,
                }
            )

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
        "team_insight": team_insight,
        "users": users,
        "user_id": user_id,
        "target_user": target_user,
        "person_insight": person_insight,
        "stats": stats,
        "per_user_stats": per_user_stats,
        "per_user_fillers": per_user_fillers,
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
