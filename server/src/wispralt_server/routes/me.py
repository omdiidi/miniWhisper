"""JSON /me endpoint for client identity self-management.

Auth: any valid Bearer token (admin or employee). Each user can only read or write
their own row — there is no path parameter.

Phase 2 (insights): extends the existing router with three Jinja2-rendered
employee-surface routes:

- ``GET  /me/login``     — token-paste form (no auth required, mints CSRF token).
- ``POST /me/login``     — verify CSRF, validate token, set session cookie, redirect.
- ``GET  /me/insights``  — authed; per-employee weekly insight + stats dashboard.

These mirror ``/admin/login`` and ``/admin/me`` but live at ``/me/*`` so the URL
communicates self-service. The session cookie name is shared with ``/admin``
(``wispralt_admin_token``) so a single login spans both surfaces; the cookie
path is set to ``/`` so it actually traverses the boundary.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import string
from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from wispralt_server import auth as auth_mod
from wispralt_server.auth import (
    require_api_key,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from wispralt_server.config import settings
from wispralt_server.constants import MAX_DISPLAY_NAME_LEN
from wispralt_server.insights.timewindow import (
    epoch_for_range,
    last_full_iso_week,
)
from wispralt_server.users import store as users_store
from wispralt_server.web.htmx import is_htmx
from wispralt_server.web.templates_env import templates

if TYPE_CHECKING:
    from wispralt_server.jobs.store import JobStore
    from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


class MeResponse(BaseModel):
    label: str
    display_name: str | None
    role: str
    created_at: str  # ISO-8601
    last_seen_at: str | None


class PatchMeRequest(BaseModel):
    display_name: str | None = Field(default=None)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not (1 <= len(v) <= MAX_DISPLAY_NAME_LEN):
            raise ValueError(f"display_name must be 1-{MAX_DISPLAY_NAME_LEN} characters")
        # Reject embedded control chars (newline, tab, NUL, etc.) — must match SQL CHECK.
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("display_name may not contain control characters")
        return v


def _profile_to_response(p: users_store.UserProfile) -> MeResponse:
    return MeResponse(
        label=p.label,
        display_name=p.display_name,
        role=p.role,
        created_at=p.created_at.isoformat(),
        last_seen_at=p.last_seen_at.isoformat() if p.last_seen_at else None,
    )


@router.get("", response_model=MeResponse)
async def get_me(
    request: Request, user: User = Depends(require_api_key)
) -> MeResponse:
    profile = await users_store.fetch_profile_by_id(
        request.app.state.db_pool, user.id
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return _profile_to_response(profile)


@router.patch("", response_model=MeResponse)
async def patch_me(
    request: Request,
    body: PatchMeRequest,
    user: User = Depends(require_api_key),
) -> MeResponse:
    pool = request.app.state.db_pool
    await users_store.update_display_name(pool, user.id, body.display_name)
    # No token-cache invalidation needed — display_name is not cached on the auth User.
    profile = await users_store.fetch_profile_by_id(pool, user.id)
    assert profile is not None  # we just authed via require_api_key, the row exists
    return _profile_to_response(profile)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: employee login + insights dashboard (Jinja2-rendered)
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def me_login_form(request: Request) -> HTMLResponse:
    """Token-paste login form for employees.

    Mints a fresh CSRF token, embeds it in the form, and sets it as a
    short-lived cookie. POST verifies cookie == form via constant-time compare.
    """
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "me_login.html.j2",
        {
            "request": request,
            "error": None,
            "hide_chrome": True,
            "csrf_token": csrf,
        },
    )
    set_csrf_cookie(response, csrf)
    return response


def _render_me_login_error(
    request: Request, *, error: str, status_code: int
) -> HTMLResponse:
    """Render the login form with an error and a freshly-minted CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "me_login.html.j2",
        {
            "request": request,
            "error": error,
            "hide_chrome": True,
            "csrf_token": csrf,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf)
    return response


@router.post("/login")
async def me_login_submit(
    request: Request,
    token: str = Form(...),
    csrf_token: str = Form(...),
):
    """Validate *token* and set the session cookie.

    Mirrors /admin/login's 3-stage lookup (cache → Postgres → break-glass) but
    rejects break-glass with a clear message instead of letting an admin
    sentinel sneak into the employee surface.
    """
    if not verify_csrf(request, csrf_token):
        return _render_me_login_error(
            request,
            error="Form session expired. Please reload and try again.",
            status_code=403,
        )

    # Token-format validation: hex64 only. Both malformed AND well-formed-but-
    # invalid get identical 401s — no length oracle.
    if len(token) != 64 or not all(c in string.hexdigits for c in token):
        return _render_me_login_error(
            request, error="Invalid or revoked token.", status_code=401,
        )

    th = users_store.hash_token(token)
    pool = getattr(request.app.state, "db_pool", None)
    user: User | None = None

    # 1. Cache fast-path
    cached = auth_mod.token_cache.get(th)
    if cached is not None:
        user = cached

    # 2. Postgres lookup
    if user is None and pool is not None:
        try:
            user = await users_store.lookup(pool, th)
        except asyncpg.PostgresError:
            logger.exception("Postgres lookup failed during /me/login")
            user = None
        if user is not None:
            auth_mod.token_cache.put(th, user)

    # 3. Break-glass: REJECTED at /me/login (admin-only surface). User gets a
    # clear message instead of authing then 403'ing at /me/insights.
    bg = getattr(request.app.state, "break_glass_token_hash", None)
    if user is None and bg is not None and th == bg:
        return _render_me_login_error(
            request,
            error="Break-glass token is admin-only — use /admin/login.",
            status_code=403,
        )

    if user is None:
        return _render_me_login_error(
            request, error="Invalid or revoked token.", status_code=401,
        )

    resp = RedirectResponse(url="/me/insights", status_code=303)
    # Same cookie name as admin — auth.py:_extract_bearer falls back to it.
    # path="/" so it scopes across /admin/* AND /me/*.
    set_session_cookie(resp, token)
    return resp


@router.get("/insights", response_class=HTMLResponse)
async def me_insights(
    request: Request,
    user: User = Depends(require_api_key),
    range: str = "7d",
):
    """Employee self-view of their last-week LLM insight + time-range stats.

    Break-glass admin (user.id < 0) redirects to /admin/data — they don't
    have a person row to insight on.
    """
    if user.id < 0:
        return RedirectResponse(url="/admin/data", status_code=303)

    iso_year, iso_week = last_full_iso_week(settings.insights_timezone)
    job_store: JobStore = request.app.state.job_store
    insight = job_store.get_weekly_insight_person(user.id, iso_year, iso_week)
    since = epoch_for_range(range, settings.insights_timezone)
    stats = await asyncio.to_thread(
        job_store.compute_user_stats, user.id, since_epoch=since,
    )

    ctx = {
        "request": request,
        "user": user,
        "insight": insight,
        "stats": stats,
        "range": range,
        "iso_year": iso_year,
        "iso_week": iso_week,
    }

    if is_htmx(request):
        return templates.TemplateResponse(
            request, "_me_insights_body.html.j2", ctx,
        )

    return templates.TemplateResponse(
        request, "me_insights.html.j2", ctx,
    )
