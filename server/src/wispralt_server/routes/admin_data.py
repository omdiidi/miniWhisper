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
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..auth import require_admin
from ..config import settings
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
        stats = None
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
    }

    if is_htmx(request):
        return templates.TemplateResponse(
            request, "_admin_data_body.html.j2", ctx,
        )

    return templates.TemplateResponse(
        request, "data.html.j2", ctx,
    )
