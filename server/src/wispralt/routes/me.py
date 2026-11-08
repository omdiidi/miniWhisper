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

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from wispralt.auth import (
    csrf_token_for,
    forbid_integration_kind,
    require_csrf,
    resolve_token,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from wispralt.config import settings
from wispralt.constants import MAX_DISPLAY_NAME_LEN
from wispralt.insights import measured, sources
from wispralt.insights import settings as insights_settings
from wispralt.insights.settings import ai_enabled
from wispralt.insights.timewindow import (
    epoch_for_range,
    iso_week_epoch_bounds,
    last_full_iso_week,
)
from wispralt.web import records
from wispralt.web.htmx import is_htmx
from wispralt.web.templates_env import templates

if TYPE_CHECKING:
    from wispralt.accounts.store import AccountStore, User, UserProfile
    from wispralt.jobs.store import JobStore

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


def _profile_to_response(p: UserProfile) -> MeResponse:
    return MeResponse(
        label=p.label,
        display_name=p.display_name,
        role=p.role,
        created_at=p.created_at.isoformat(),
        last_seen_at=p.last_seen_at.isoformat() if p.last_seen_at else None,
    )


@router.get("", response_model=MeResponse)
async def get_me(
    request: Request, user: User = Depends(forbid_integration_kind)
) -> MeResponse:
    accounts: AccountStore = request.app.state.accounts
    profile = await asyncio.to_thread(accounts.profile, user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return _profile_to_response(profile)


@router.patch("", response_model=MeResponse)
async def patch_me(
    request: Request,
    body: PatchMeRequest,
    user: User = Depends(forbid_integration_kind),
    _csrf: User = Depends(require_csrf),
) -> MeResponse:
    accounts: AccountStore = request.app.state.accounts
    await asyncio.to_thread(accounts.update_display_name, user.id, body.display_name)
    profile = await asyncio.to_thread(accounts.profile, user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    return _profile_to_response(profile)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: employee login + insights dashboard (Jinja2-rendered)
# ─────────────────────────────────────────────────────────────────────────────


def _safe_next(raw: str | None) -> str | None:
    """Sanitize a ?next= URL param for use with /me/login.

    Returns the raw value when it (a) starts with "/me/" AND (b) contains
    no `..` path segments. Else returns None.

    This prevents both open-redirect and path-traversal browser-normalize
    bypasses (e.g. "/me/../admin/data" → "/admin/data" after RFC 3986 §5.2.4
    normalization). Even though require_admin would 403 such requests, we
    don't want our Location header to carry an attacker-controlled path.
    """
    if raw is None:
        return None
    if not raw.startswith("/me/"):
        return None
    # Defense-in-depth: reject any percent-encoded form of `.` or `/` that
    # could decode to a path-traversal segment AFTER our check. Belt-and-
    # suspenders alongside the literal `..` rejection below — protects
    # against a future `/me/{path:path}` catch-all route being added.
    lowered = raw.lower()
    if "%2e" in lowered or "%2f" in lowered:
        return None
    # Reject any `..` path segment (path-traversal in URL string form).
    parts = raw.split("/")
    if ".." in parts:
        return None
    return raw


@router.get("/login", response_class=HTMLResponse)
async def me_login_form(request: Request) -> HTMLResponse:
    """Token-paste login form for employees.

    Mints a fresh CSRF token, embeds it in the form, and sets it as a
    short-lived cookie. POST verifies cookie == form via constant-time compare.

    Honors an optional ``?next=`` query param (e.g. ``?next=/me/history``) so
    the menubar "Open My Dictations" deep-link survives the login round-trip.
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
            "next": _safe_next(request.query_params.get("next")),
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
            "next": _safe_next(request.query_params.get("next")),
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

    Same lookup as /admin/login (:func:`resolve_token`).
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

    user = await resolve_token(request.app, token)
    if user is None:
        return _render_me_login_error(
            request, error="Invalid or revoked token.", status_code=401,
        )

    # v3: honor ?next= query param for menubar deep-link from "Open My Dictations"
    # button (lands on /me/history after auth instead of default /me/insights).
    # Safety: only allow relative same-origin paths under /me/ to prevent
    # open-redirect AND path-traversal (see _safe_next docstring).
    next_url = _safe_next(request.query_params.get("next")) or "/me/insights"
    resp = RedirectResponse(url=next_url, status_code=303)
    # Same cookie name as admin — auth.py:_extract_bearer falls back to it.
    # path="/" so it scopes across /admin/* AND /me/*.
    set_session_cookie(resp, token)
    return resp


@router.get("/insights", response_class=HTMLResponse)
async def me_insights(
    request: Request,
    user: User = Depends(forbid_integration_kind),
    range: str = "7d",
):
    """Employee self-view of their last-week LLM insight + time-range stats.

    """

    tz = await insights_settings.timezone_for(request.app)
    iso_year, iso_week = last_full_iso_week(tz)
    job_store: JobStore = request.app.state.job_store
    row = job_store.weekly_insight(user.id, iso_year, iso_week)
    ai = await asyncio.to_thread(
        lambda: sources.build_view(
            job_store, row, viewer_id=user.id, is_admin=False, scope_user_id=user.id,
            bounds=iso_week_epoch_bounds(iso_year, iso_week, tz), iso_week=(iso_year, iso_week),
            link_base="/me/history",
        )
    )
    now = time.time()
    since = epoch_for_range(range, tz)
    m = await asyncio.to_thread(
        lambda: measured.compute(
            job_store, request.app.state.usage,
            user_id=user.id, since=since, until=now, now=now, tz_name=tz,
        )
    )

    ctx = {
        "request": request,
        "user": user,
        "ai": ai,
        "ai_enabled": await ai_enabled(request.app),
        "ai_team": False,
        "viewer_is_admin": user.role == "admin",
        "m": m,
        "link_base": "/me/history",
        "link_params": {},
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
    return records.decorate(row, tz_name)


@router.get("/history", response_class=HTMLResponse)
async def me_history(
    request: Request,
    user: User = Depends(forbid_integration_kind),
    range: str = "30d",
    kind: str | None = None,
    q: str | None = None,
    since: float | None = None,
    until: float | None = None,
    dict_cursor: str | None = None,
    jobs_cursor: str | None = None,
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    """Personal transcript archive (dictations + file transcriptions).

    With ``q`` of 2+ characters it is a full-text search (FTS5, ``search_history`` —
    scoped to this user in SQL) with highlighted snippets and offset paging; otherwise
    the plain newest-first list with per-leg cursors. ``since``/``until`` (unix seconds)
    override the ``range`` tab (the measured-stats tiles link here with them).
    """
    if kind not in ("dictation", "file"):
        kind = None
    tz = await insights_settings.timezone_for(request.app)
    lo, hi = records.window(range, since, until, tz)
    query = records.wants_search(q)
    job_store: JobStore = request.app.state.job_store
    page = settings.history_page_size
    next_dc = next_jc = next_offset = None

    if query is not None:
        hits = await asyncio.to_thread(
            job_store.search_history, user.id, query,
            since=lo, until=hi, kind=kind, limit=page + 1, offset=offset,
        )
        rows = [records.hit_row(h, tz) for h in hits[:page]]
        next_offset = offset + page if len(hits) > page else None
    else:
        dc = _decode_cursor(dict_cursor)
        jc = _decode_cursor(jobs_cursor)
        raw, ndc, njc = await asyncio.to_thread(
            job_store.transcripts_in_range_filtered,
            user.id,
            since_epoch=lo,
            until_epoch=hi,
            kind=kind,
            dict_cursor=dc,
            jobs_cursor=jc,
            limit=page,
        )
        rows = [_decorate_row(r, tz) for r in raw]
        next_dc, next_jc = _encode_cursor(ndc), _encode_cursor(njc)

    if dict_cursor or jobs_cursor or offset:
        template_name = "_me_history_page.html.j2"
    elif is_htmx(request):
        template_name = "_me_history_body.html.j2"
    else:
        template_name = "me_history.html.j2"

    csrf = secrets.token_urlsafe(32)
    ctx = {
        "request": request,
        "user": user,
        "rows": rows,
        "next_dict_cursor": next_dc,
        "next_jobs_cursor": next_jc,
        "next_offset": next_offset,
        "range": range,
        "kind": kind,
        "q": q or "",
        "searching": query is not None,
        "filter_vals": {
            k: v
            for k, v in {"range": range, "kind": kind, "q": q, "since": since, "until": until}.items()
            if v not in (None, "")
        },
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
    user: User = Depends(forbid_integration_kind),
    compact: bool = False,
) -> HTMLResponse:
    """One owned record: the expanded (or ``?compact=1``) table-row partial for HTMX,
    else a full record page (the target of insight source links).

    Unknown ``kind`` and another user's ``row_id`` both return 404 (no existence leak).
    """
    job_store: JobStore = request.app.state.job_store
    row = await asyncio.to_thread(job_store.get_history_row, user.id, kind, row_id)
    if row is None:
        raise HTTPException(status_code=404)

    decorated = _decorate_row(row, await insights_settings.timezone_for(request.app))
    decorated["kind"] = kind
    decorated["row_id"] = row_id
    csrf = csrf_token_for(request)  # never rotate: the page's other forms hold this token
    if compact:
        template_name = "_me_history_row.html.j2"
    elif is_htmx(request):
        template_name = "_me_history_row_expanded.html.j2"
    else:
        template_name = "record_detail.html.j2"
    ctx = {
        "request": request,
        "row": decorated,
        "kind": kind,
        "csrf_token": csrf,
        "admin_view": False,
    }
    response = templates.TemplateResponse(request, template_name, ctx)
    set_csrf_cookie(response, csrf)
    return response


@router.delete("/history/{kind}/{row_id}")
async def me_history_delete(
    request: Request,
    kind: str,
    row_id: str,
    user: User = Depends(forbid_integration_kind),
    _csrf: User = Depends(require_csrf),
) -> Response:
    """Soft-delete one owned row. Returns an HTMX OOB delete fragment."""
    if kind not in ("dictation", "file"):
        raise HTTPException(status_code=404)

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
    user: User = Depends(forbid_integration_kind),
    _csrf: User = Depends(require_csrf),
) -> HTMLResponse:
    """Restore one soft-deleted owned row. Returns the compact row partial."""
    if kind not in ("dictation", "file"):
        raise HTTPException(status_code=404)

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

    decorated = _decorate_row(row, await insights_settings.timezone_for(request.app))
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
    user: User = Depends(forbid_integration_kind),
) -> Response:
    """Plain-text or JSON download of one owned row."""
    if kind not in ("dictation", "file"):
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
            float(row["created_at"]), ZoneInfo(await insights_settings.timezone_for(request.app)),
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
