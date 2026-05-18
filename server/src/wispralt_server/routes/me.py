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
import base64
import binascii
import json
import logging
import secrets
import string
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
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
        except asyncpg.Error:
            # asyncpg.Error covers both PostgresError AND InterfaceError ("pool is
            # closed"). The narrower catch would let InterfaceError escape and 500
            # the request even though the break-glass fallback below could serve it.
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


# ─────────────────────────────────────────────────────────────────────────────
# Plan A: /me/history — personal transcript archive with search/filter/download
# ─────────────────────────────────────────────────────────────────────────────


def _encode_cursor(cur: tuple[float, str] | None) -> str | None:
    """Base64-urlsafe encode a (epoch, row_id) cursor for round-trip in URLs."""
    if cur is None:
        return None
    raw = f"{cur[0]}:{cur[1]}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(s: str | None) -> tuple[float, str] | None:
    """Decode a base64-urlsafe cursor. Malformed → ``None`` (restart pagination)."""
    if not s:
        return None
    try:
        raw = base64.urlsafe_b64decode(s.encode()).decode()
        epoch_str, _, row_id = raw.partition(":")
        return (float(epoch_str), row_id)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def _decorate_row(row: dict, tz_name: str) -> dict:
    """Attach an ISO-formatted timestamp for template render. Pure; returns new dict."""
    ts = row.get("created_at")
    iso = ""
    if ts is not None:
        try:
            iso = datetime.fromtimestamp(
                float(ts), ZoneInfo(tz_name),
            ).isoformat(timespec="minutes")
        except (ValueError, OSError):
            iso = ""
    return {**row, "created_at_iso": iso}


@router.get("/history", response_class=HTMLResponse)
async def me_history(
    request: Request,
    user: User = Depends(require_api_key),
    range: str = "30d",
    kind: str | None = None,
    search: str | None = None,
    dict_cursor: str | None = None,
    jobs_cursor: str | None = None,
) -> HTMLResponse:
    """Personal transcript archive (dictations + meetings).

    Break-glass admins (``user.id < 0``) have no person row, so they get
    redirected to ``/admin/data``. Search shorter than 3 chars is silently
    dropped to keep the LIKE-scan cost bounded.
    """
    if user.id < 0:
        return RedirectResponse(url="/admin/data", status_code=303)

    if search is not None:
        search = search.strip() or None
    if search and len(search) < 3:
        search = None

    # Normalize empty-string kind from form-select to None
    if kind == "":
        kind = None

    since = epoch_for_range(range, settings.insights_timezone)
    until = time.time()

    dc = _decode_cursor(dict_cursor)
    jc = _decode_cursor(jobs_cursor)

    job_store: JobStore = request.app.state.job_store
    rows, next_dc, next_jc = await asyncio.to_thread(
        job_store.transcripts_in_range_filtered,
        user.id,
        since_epoch=since,
        until_epoch=until,
        kind=kind,
        search=search,
        dict_cursor=dc,
        jobs_cursor=jc,
        limit=settings.history_page_size,
    )

    decorated = [_decorate_row(r, settings.insights_timezone) for r in rows]

    is_loadmore = bool(dc or jc)
    if is_loadmore:
        template_name = "_me_history_page.html.j2"
    elif is_htmx(request):
        template_name = "_me_history_body.html.j2"
    else:
        template_name = "me_history.html.j2"

    csrf = secrets.token_urlsafe(32)
    ctx = {
        "request": request,
        "user": user,
        "rows": decorated,
        "next_dict_cursor": _encode_cursor(next_dc),
        "next_jobs_cursor": _encode_cursor(next_jc),
        "range": range,
        "kind": kind,
        "search": search,
        "csrf_token": csrf,
    }
    response = templates.TemplateResponse(request, template_name, ctx)
    set_csrf_cookie(response, csrf)
    return response


@router.get("/history/{kind}/{row_id}", response_class=HTMLResponse)
async def me_history_row(
    request: Request,
    kind: str,
    row_id: str,
    user: User = Depends(require_api_key),
    compact: bool = False,
) -> HTMLResponse:
    """Expanded (or compact, if ``?compact=1``) single-row partial.

    Unknown ``kind`` or non-owned ``row_id`` both return 404 uniformly so we
    don't leak existence to other users.
    """
    if user.id < 0:
        raise HTTPException(status_code=404)
    if kind not in ("dictation", "meeting"):
        raise HTTPException(status_code=404)

    job_store: JobStore = request.app.state.job_store
    row = await asyncio.to_thread(
        job_store.get_history_row, user.id, kind, row_id,
    )
    if row is None:
        raise HTTPException(status_code=404)

    decorated = _decorate_row(row, settings.insights_timezone)
    decorated["kind"] = kind
    decorated["row_id"] = row_id

    csrf = secrets.token_urlsafe(32)
    template_name = (
        "_me_history_row.html.j2" if compact
        else "_me_history_row_expanded.html.j2"
    )
    ctx = {
        "request": request,
        "row": decorated,
        "kind": kind,
        "csrf_token": csrf,
    }
    response = templates.TemplateResponse(request, template_name, ctx)
    set_csrf_cookie(response, csrf)
    return response


@router.delete("/history/{kind}/{row_id}")
async def me_history_delete(
    request: Request,
    kind: str,
    row_id: str,
    csrf_token: str = Form(...),
    user: User = Depends(require_api_key),
) -> Response:
    """Soft-delete one owned row. Returns an HTMX OOB delete fragment."""
    if user.id < 0:
        raise HTTPException(status_code=404)
    if kind not in ("dictation", "meeting"):
        raise HTTPException(status_code=404)
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403)

    job_store: JobStore = request.app.state.job_store
    ok = await asyncio.to_thread(
        job_store.soft_delete_history_row, user.id, kind, row_id,
    )
    if not ok:
        raise HTTPException(status_code=404)

    return HTMLResponse(
        f'<tr id="row-{kind}-{row_id}" hx-swap-oob="delete"></tr>',
        media_type="text/html",
    )


@router.post("/history/{kind}/{row_id}/restore", response_class=HTMLResponse)
async def me_history_restore(
    request: Request,
    kind: str,
    row_id: str,
    csrf_token: str = Form(...),
    user: User = Depends(require_api_key),
) -> HTMLResponse:
    """Restore one soft-deleted owned row. Returns the compact row partial."""
    if user.id < 0:
        raise HTTPException(status_code=404)
    if kind not in ("dictation", "meeting"):
        raise HTTPException(status_code=404)
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=403)

    job_store: JobStore = request.app.state.job_store
    ok = await asyncio.to_thread(
        job_store.restore_history_row, user.id, kind, row_id,
    )
    if not ok:
        raise HTTPException(status_code=404)

    # Re-fetch so the template has the full row context.
    row = await asyncio.to_thread(
        job_store.get_history_row, user.id, kind, row_id,
    )
    if row is None:
        # Race: deleted again between restore + read. Treat as 404.
        raise HTTPException(status_code=404)

    decorated = _decorate_row(row, settings.insights_timezone)
    decorated["kind"] = kind
    decorated["row_id"] = row_id

    csrf = secrets.token_urlsafe(32)
    ctx = {
        "request": request,
        "row": decorated,
        "kind": kind,
        "csrf_token": csrf,
    }
    response = templates.TemplateResponse(request, "_me_history_row.html.j2", ctx)
    set_csrf_cookie(response, csrf)
    return response


@router.get("/history/{kind}/{row_id}/download/{fmt}")
async def me_history_download(
    request: Request,
    kind: str,
    row_id: str,
    fmt: str,
    user: User = Depends(require_api_key),
) -> Response:
    """Plain-text or JSON download of one owned row."""
    if user.id < 0:
        raise HTTPException(status_code=404)
    if kind not in ("dictation", "meeting"):
        raise HTTPException(status_code=404)
    if fmt not in ("txt", "json"):
        raise HTTPException(status_code=404)

    job_store: JobStore = request.app.state.job_store
    row = await asyncio.to_thread(
        job_store.get_history_row, user.id, kind, row_id,
    )
    if row is None:
        raise HTTPException(status_code=404)

    try:
        iso_date = datetime.fromtimestamp(
            float(row["created_at"]), ZoneInfo(settings.insights_timezone),
        ).date().isoformat()
    except (ValueError, OSError, KeyError, TypeError):
        iso_date = "unknown"

    fname = f"{kind}-{iso_date}-{row_id}.{fmt}"
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}

    if fmt == "txt":
        content = row.get("text") or row.get("transcript_text") or ""
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )
    # fmt == "json"
    return Response(
        content=json.dumps(row, default=str),
        media_type="application/json",
        headers=headers,
    )
