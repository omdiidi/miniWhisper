"""
routes/admin_ui.py — Server-rendered Jinja2 admin UI under ``/admin/*``.

Two routers under the same prefix:

- :data:`public_router` exposes ``/admin/login`` (GET form + POST submit) —
  these endpoints MUST be reachable without auth, otherwise the operator
  has no way to acquire the session cookie that gates the rest of the UI.
- :data:`authed_router` covers everything else and is gated by
  :func:`require_admin`.
- :data:`me_router` serves ``/admin/me`` to any authenticated user.

Every page reads the local SQLite DB through ``app.state.accounts``
(:class:`AccountStore`) and ``app.state.usage`` (:class:`UsageStore`), each
call in a worker thread. Tokens are generated only by ``accounts.service``.

Auth model
----------
1. ``Authorization: Bearer ...`` — for curl / extension users.  Resolves
   via the same path as the dictation endpoints.
2. ``wispralt_admin_token`` cookie — for browser navigation; set by
   ``POST /admin/login`` with ``HttpOnly``, ``Secure``, ``SameSite=Strict``.
   ``_extract_bearer`` (in ``auth.py``) falls back to the cookie when the
   header is absent.

The login POST validates the token with :func:`auth.resolve_token` (the same
lookup the auth dependency uses). We do NOT call ``require_api_key``
recursively because Starlette's ``request.cookies`` is read-only — writing
into it has no effect on the very next dependency that would re-read it.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import setup_code
from .._errors import AlreadyBootstrapped
from ..accounts import service as account_service
from ..accounts.store import AccountStore, User
from ..auth import (
    require_admin,
    require_admin_csrf,
    require_api_key,
    resolve_token,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from ..config import settings
from ..constants import MAX_DISPLAY_NAME_LEN
from ..usage.store import UsageFilters, UsageStore
from ..web.templates_env import templates

logger = logging.getLogger(__name__)


def _accounts(request: Request) -> AccountStore:
    return request.app.state.accounts


def _form_page(
    request: Request, template: str, ctx: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """Render a page that holds a mutating form: mint a CSRF token, embed it, set its cookie."""
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        template, {"request": request, "csrf_token": csrf, **ctx}, status_code=status_code
    )
    set_csrf_cookie(response, csrf)
    return response


def _usage(request: Request) -> UsageStore:
    return request.app.state.usage


# Two routers under the same /admin prefix.
public_router = APIRouter(prefix="/admin")
authed_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
# Self-only router: any authenticated user (admin OR employee) can hit /admin/me
# to see their own usage.  Gated by require_api_key (not require_admin) so
# employees don't get 403'd out of their own page.
me_router = APIRouter(prefix="/admin")


# ── /admin/login (unauthenticated) ─────────────────────────────────────────────


@public_router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    """Render the single-field login form with a fresh CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        "login.html.j2",
        {"request": request, "csrf_token": csrf},
    )
    set_csrf_cookie(response, csrf)
    return response


def _render_admin_login_error(
    request: Request, *, error: str, status_code: int
) -> HTMLResponse:
    """Render the admin login form with an error + freshly-minted CSRF token."""
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        "login.html.j2",
        {"request": request, "error": error, "csrf_token": csrf},
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf)
    return response


@public_router.post("/login")
async def login_submit(
    request: Request,
    token: str = Form(...),
    csrf_token: str = Form(...),
) -> Any:
    """Validate *token* (:func:`resolve_token`) and set the session cookie on success."""
    if not verify_csrf(request, csrf_token):
        return _render_admin_login_error(
            request,
            error="Form session expired. Please reload and try again.",
            status_code=403,
        )

    user = await resolve_token(request.app, token)
    if user is None:
        return _render_admin_login_error(
            request, error="Invalid token", status_code=401,
        )

    # Role-based landing: admins see the global dashboard; employees see only
    # their own usage page (/admin/me).  Both share the same login form so the
    # client just opens "<server>/admin/login" without knowing the role ahead
    # of time.
    target = "/admin/" if user.role == "admin" else "/admin/me"
    resp = RedirectResponse(target, status_code=303)
    # Shared helper clears the legacy /admin/login-scoped cookie and sets the
    # unified path="/" session cookie so it scopes across /admin/* AND /me/*.
    set_session_cookie(resp, token)
    return resp


# ── /admin/setup — portal first-admin setup (unauthenticated, gated) ─────────
#
# Exists only while no enabled admin exists (else 404) and only with the code from
# ``data_dir/setup-code`` (written at startup by ``setup_code.prepare``). The race
# between two submitters is settled by ``bootstrap_first_admin`` (``AlreadyBootstrapped``).

NO_STORE = {"Cache-Control": "no-store"}


async def _setup_open(request: Request) -> None:
    if await asyncio.to_thread(_accounts(request).has_enabled_admin):
        raise HTTPException(status_code=404)


def _setup_form(request: Request, *, error: str | None = None, label: str = "owner",
                status_code: int = 200) -> HTMLResponse:
    return _form_page(
        request,
        "setup.html.j2",
        {"hide_chrome": True, "error": error, "form_label": label,
         "code_file": setup_code.FILENAME},
        status_code=status_code,
    )


@public_router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request) -> HTMLResponse:
    await _setup_open(request)
    return _setup_form(request)


@public_router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    code: str = Form(...),
    label: str = Form("owner"),
    csrf_token: str = Form(...),
) -> HTMLResponse:
    """Create the first admin with the on-disk setup code; show its key once."""
    if not verify_csrf(request, csrf_token):
        return _setup_form(request, error="Form expired. Reload and try again.", status_code=403)
    await _setup_open(request)
    clean_label, err = _validate_label(label)
    if err is not None:
        return _setup_form(request, error=err, label=label, status_code=400)
    if not setup_code.matches(settings.data_dir, code):
        logger.warning("admin setup refused: wrong or missing setup code")
        return _setup_form(request, error="That setup code is not valid.", label=label,
                           status_code=403)
    try:
        user, plaintext = await asyncio.to_thread(
            account_service.bootstrap_first_admin, _accounts(request), clean_label
        )
    except AlreadyBootstrapped as exc:
        raise HTTPException(status_code=404) from exc
    setup_code.consume(settings.data_dir)
    response = templates.TemplateResponse(
        "setup.html.j2",
        {"request": request, "hide_chrome": True, "created": user, "plaintext": plaintext,
         "server_url": settings.server_url},
        headers=NO_STORE,
    )
    set_session_cookie(response, plaintext)
    return response


# ── authed routes ─────────────────────────────────────────────────────────────


@authed_router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    now = time.time()
    stats = await asyncio.to_thread(_usage(request).overview, now)
    # "Dictations" tiles count dictation rows (a streamed dictation is one row,
    # however many chunk requests it took); usage_events stays request telemetry.
    counts = await asyncio.to_thread(request.app.state.job_store.dictation_counts, now)
    stats["totals"].update(
        dictations_24h=counts["24h"], dictations_7d=counts["7d"], dictations_30d=counts["30d"]
    )
    integration_count = await asyncio.to_thread(_accounts(request).count_kind, "integration")
    return templates.TemplateResponse(
        "overview.html.j2",
        {"request": request, "integration_count": integration_count, **stats},
    )


@authed_router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request) -> HTMLResponse:
    rows = await asyncio.to_thread(_accounts(request).list_people)
    return _form_page(request, "users.html.j2", {"users": rows})


_VALID_ROLES = {"admin", "employee"}
_LABEL_MAX_LEN = 80


def _validate_label(raw: str) -> tuple[str | None, str | None]:
    """Trim *raw* and reject empty / too-long / control-char labels.

    Returns ``(label, None)`` on success, ``(None, error)`` on failure.
    """
    if raw is None:
        return None, "Label is required."
    label = raw.strip()
    if not label:
        return None, "Label is required."
    if len(label) > _LABEL_MAX_LEN:
        return None, f"Label too long ({len(label)} chars; max {_LABEL_MAX_LEN})."
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in label):
        return None, "Label contains control characters."
    return label, None


def _validate_optional_display_name(raw: str) -> tuple[str | None, str | None]:
    """Trim *raw*; treat empty as None (admin opted out). Otherwise apply the
    same rules as ``routes/me.py:PatchMeRequest.validate_display_name`` —
    length 1..MAX_DISPLAY_NAME_LEN, no control characters. Returns
    ``(cleaned_or_None, None)`` on success, ``(None, error)`` on failure.
    """
    if raw is None:
        return None, None
    cleaned = raw.strip()
    if not cleaned:
        return None, None
    if len(cleaned) > MAX_DISPLAY_NAME_LEN:
        return None, (
            f"Display name too long ({len(cleaned)} chars; "
            f"max {MAX_DISPLAY_NAME_LEN})."
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        return None, "Display name contains control characters."
    return cleaned, None


def _build_install_command(server_url: str, plaintext: str) -> str:
    """Compose the curl one-liner. Env vars sit on the bash side of the pipe
    (the curl-side form drops them — see CHANGELOG.md [0.1.1]).
    """
    # -q makes curl ignore the employee's ~/.curlrc, so a malformed config (a stray
    # url/proxy line) can't make this fetch print "curl: (6) Could not resolve host:"
    # or fail before the script runs.
    return (
        "curl -q -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \\\n"
        f"  | WISPRALT_API_KEY={plaintext} WISPRALT_SERVER={server_url} bash"
    )


@authed_router.get("/users/new", response_class=HTMLResponse)
async def users_add_form(request: Request) -> HTMLResponse:
    """Render the add-employee form."""
    return _form_page(
        request,
        "add_employee.html.j2",
        {"form_label": "", "form_role": "employee", "form_display_name": ""},
    )


# The six admin mutations take their user through ``require_admin_csrf`` as a
# PARAMETER dependency (auth first, then CSRF; a valid Bearer skips the token).


@authed_router.post("/users/new", response_class=HTMLResponse)
async def users_add_submit(
    request: Request,
    label: str = Form(...),
    role: str = Form("employee"),
    display_name: str = Form(""),
    _admin: User = Depends(require_admin_csrf),
) -> HTMLResponse:
    """Create a new user, mint a token, and render the install one-liner.

    ``display_name`` is optional. When provided, it is pre-set on the row so
    the employee's :class:`FirstLaunchCoordinator` skips its name-sheet prompt.
    """
    clean_label, err = _validate_label(label)
    clean_display_name, dn_err = _validate_optional_display_name(display_name)
    if err is None and dn_err is not None:
        err = dn_err
    if err is not None or role not in _VALID_ROLES:
        if err is None:
            err = f"Invalid role: {role!r}."
        return _form_page(
            request,
            "add_employee.html.j2",
            {
                "error": err,
                "form_label": label,
                "form_role": role if role in _VALID_ROLES else "employee",
                "form_display_name": display_name,
            },
            status_code=400,
        )

    user, plaintext = await asyncio.to_thread(
        lambda: account_service.create_user(
            _accounts(request),
            label=clean_label,
            role=role,
            kind="employee",
            display_name=clean_display_name,
        )
    )
    return templates.TemplateResponse(
        "employee_added.html.j2",
        {
            "request": request,
            "user_id": user.id,
            "label": user.label,
            "role": user.role,
            "display_name": clean_display_name,
            "plaintext": plaintext,
            "install_command": _build_install_command(settings.server_url, plaintext),
        },
    )


@authed_router.post("/users/{user_id}/revoke")
async def users_revoke(
    request: Request, user_id: int, _admin: User = Depends(require_admin_csrf)
) -> RedirectResponse:
    """Disable *user_id*: none of its keys authenticate from the next request."""
    await asyncio.to_thread(_accounts(request).disable_user, user_id)
    return RedirectResponse("/admin/users", status_code=303)


@authed_router.post("/users/{user_id}/mint", response_class=HTMLResponse)
async def users_mint(
    request: Request, user_id: int, _admin: User = Depends(require_admin_csrf)
) -> HTMLResponse:
    """Revoke the user's keys, issue one new key (re-enabling the user); show it once."""
    new_plaintext = await _rotate_or_404(request, user_id)
    return templates.TemplateResponse(
        "token_minted.html.j2",
        {
            "request": request,
            "user_id": user_id,
            "plaintext": new_plaintext,
        },
    )


async def _rotate_or_404(request: Request, user_id: int) -> str:
    """``service.rotate_user_key`` → the new plaintext; 404 for an unknown user."""
    try:
        _user, plaintext = await asyncio.to_thread(
            account_service.rotate_user_key, _accounts(request), user_id
        )
    except sqlite3.IntegrityError as exc:  # FK: no such user
        raise HTTPException(status_code=404, detail="User not found") from exc
    return plaintext


# ── /admin/keys — integration API keys (OpenAI-compat /v1 callers) ────────────
#
# Distinct from /admin/users (humans) — these are tokens issued for third-party
# programs (Buzz, MacWhisper, Open WebUI, etc.) that talk to /v1/audio/*.
# Under the hood: a users row with role='employee' and kind='integration'
# (plus its api_keys row), which both (a) hides them from /admin/users and
# (b) blocks them from /me/* via forbid_integration_kind.


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_KEY_LABEL_PREFIX = "key-"
# _LABEL_MAX_LEN is 80; prefix is 5 chars, so slug must fit in 75. Cap at 70
# to leave a little headroom and produce predictable labels.
_KEY_SLUG_MAX_LEN = 70


def _slugify_program_name(raw: str) -> str:
    """Lowercase + collapse non-[a-z0-9] runs to ``-`` + trim leading/trailing
    dashes + truncate to ``_KEY_SLUG_MAX_LEN`` chars. Returns ``""`` when the
    input has no alphanumeric content (caller treats that as a validation
    failure)."""
    lowered = raw.lower()
    collapsed = _SLUG_PATTERN.sub("-", lowered).strip("-")
    return collapsed[:_KEY_SLUG_MAX_LEN]


@authed_router.get("/keys", response_class=HTMLResponse)
async def keys_list(request: Request) -> HTMLResponse:
    """List all non-revoked integration keys (kind='integration')."""
    rows = await asyncio.to_thread(_accounts(request).list_integrations)
    return _form_page(request, "keys.html.j2", {"keys": rows})


@authed_router.get("/keys/new", response_class=HTMLResponse)
async def keys_add_form(request: Request) -> HTMLResponse:
    """Render the add-integration-key form."""
    return _form_page(request, "add_key.html.j2", {"form_program_name": ""})


@authed_router.post("/keys/new", response_class=HTMLResponse)
async def keys_add_submit(
    request: Request,
    program_name: str = Form(...),
    _admin: User = Depends(require_admin_csrf),
) -> HTMLResponse:
    """Mint an integration API key tied to a third-party program name.

    Steps:
        1. Validate ``program_name`` (1..MAX_DISPLAY_NAME_LEN chars, no
           control chars). Reuses ``_validate_optional_display_name`` but
           treats empty as an error here (Add Employee allows empty).
        2. Slugify the name into ``label = "key-<slug>"``.
        3. Create the integration user + key in one transaction
           (role='employee', kind='integration', display_name=program_name).
        4. Render the key_added page with the plaintext + OpenAI env-var
           snippet.
    """
    clean_name, err = _validate_optional_display_name(program_name)
    if err is not None or not clean_name:
        return _form_page(
            request,
            "add_key.html.j2",
            {"error": err or "Program name is required.", "form_program_name": program_name},
            status_code=400,
        )

    slug = _slugify_program_name(clean_name)
    if not slug:
        return _form_page(
            request,
            "add_key.html.j2",
            {
                "error": "Program name must contain at least one letter or digit.",
                "form_program_name": program_name,
            },
            status_code=400,
        )

    label = f"{_KEY_LABEL_PREFIX}{slug}"

    user, plaintext = await asyncio.to_thread(
        lambda: account_service.create_user(
            _accounts(request),
            label=label,
            role="employee",
            kind="integration",
            display_name=clean_name,
        )
    )

    return templates.TemplateResponse(
        "key_added.html.j2",
        {
            "request": request,
            "mode": "mint",
            "program_name": clean_name,
            "plaintext": plaintext,
            "label": label,
            "kind": user.kind,
            "server_url": settings.server_url.rstrip("/"),
        },
    )


@authed_router.post("/keys/{user_id}/revoke")
async def keys_revoke(
    request: Request, user_id: int, _admin: User = Depends(require_admin_csrf)
) -> RedirectResponse:
    """Revoke an integration key (disables its user; applies to the next request)."""
    await asyncio.to_thread(_accounts(request).disable_user, user_id)
    return RedirectResponse("/admin/keys", status_code=303)


@authed_router.post("/keys/{user_id}/rotate", response_class=HTMLResponse)
async def keys_rotate(
    request: Request, user_id: int, _admin: User = Depends(require_admin_csrf)
) -> HTMLResponse:
    """Rotate an integration key in place; show the new plaintext once."""
    new_plaintext = await _rotate_or_404(request, user_id)
    # Re-fetch so we can show the original program name on the success page.
    profile = await asyncio.to_thread(_accounts(request).profile, user_id)
    program_name = profile.display_name if profile is not None else None
    label = profile.label if profile is not None else None

    return templates.TemplateResponse(
        "key_added.html.j2",
        {
            "request": request,
            "mode": "rotate",
            "program_name": program_name,
            "plaintext": new_plaintext,
            "label": label,
            "kind": "integration",
            "server_url": settings.server_url.rstrip("/"),
        },
    )


async def _render_user_detail(request: Request, user_id: int) -> HTMLResponse:
    """Shared body for the admin /admin/users/{id} and self-only /admin/me routes."""
    user = await asyncio.to_thread(_accounts(request).profile, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    usage = _usage(request)
    totals = await asyncio.to_thread(usage.user_totals, user_id, time.time())
    events = await asyncio.to_thread(usage.recent_events, user_id, 50)
    return templates.TemplateResponse(
        "user_detail.html.j2",
        {
            "request": request,
            "user": user,
            "totals": totals,
            "events": events,
        },
    )


@authed_router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int) -> HTMLResponse:
    return await _render_user_detail(request, user_id)


@me_router.get("/me", response_class=HTMLResponse)
async def me(request: Request, user: User = Depends(require_api_key)) -> Any:
    """Self-only landing page.

    Admins are redirected to the global dashboard so /admin/me is a safe
    universal entry point regardless of role — that lets the macOS client
    open ``<server>/admin/login`` without needing to know the role first.
    Employees see their own user_detail page (24h/7d/30d tiles + last 50
    events) with the admin nav links hidden by base.html.j2.
    """
    if user.role == "admin":
        return RedirectResponse("/admin/", status_code=303)
    return await _render_user_detail(request, user.id)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _filters(
    kind: str | None,
    status: int | None,
    user_id: int | None,
    since: str | None,
    until: str | None,
) -> UsageFilters:
    return UsageFilters(
        kind=kind or None,
        status=status,
        user_id=user_id,
        since=_parse_dt(since),
        until=_parse_dt(until),
    )


_PAGE_SIZE = 100


@authed_router.get("/usage", response_class=HTMLResponse)
async def usage_drilldown(
    request: Request,
    kind: str | None = Query(None),
    status: int | None = Query(None),
    user_id: int | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    filters = _filters(kind, status, user_id, since, until)
    rows = await asyncio.to_thread(_usage(request).events, filters, _PAGE_SIZE + 1, offset)
    has_more = len(rows) > _PAGE_SIZE
    page_rows = rows[:_PAGE_SIZE]

    raw_filters = {
        "kind": kind or "",
        "status": status,
        "user_id": user_id,
        "since": since or "",
        "until": until or "",
    }

    def _query(extra: dict[str, Any]) -> str:
        merged: dict[str, Any] = {k: v for k, v in raw_filters.items() if v not in (None, "")}
        merged.update(extra)
        return urllib.parse.urlencode(
            {k: v for k, v in merged.items() if v not in (None, "")}
        )

    next_offset = offset + _PAGE_SIZE if has_more else None
    prev_offset = offset - _PAGE_SIZE if offset > 0 else None

    return templates.TemplateResponse(
        "usage.html.j2",
        {
            "request": request,
            "events": page_rows,
            "filters": raw_filters,
            "csv_query": _query({}),
            "next_offset": next_offset,
            "prev_offset": prev_offset,
            "next_query": _query({"offset": next_offset}) if next_offset is not None else "",
            "prev_query": _query({"offset": prev_offset}) if prev_offset is not None else "",
        },
    )


@authed_router.get("/usage.csv")
async def usage_csv(
    request: Request,
    kind: str | None = Query(None),
    status: int | None = Query(None),
    user_id: int | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
) -> StreamingResponse:
    """Stream the filtered usage events as CSV."""
    filters = _filters(kind, status, user_id, since, until)
    usage = _usage(request)
    rows = await asyncio.to_thread(lambda: list(usage.iter_events(filters)))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "user_id",
            "user_label",
            "ts",
            "kind",
            "status",
            "duration_ms",
            "chars",
            "bytes_in",
            "bytes_out",
            "error_class",
            "request_id",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["user_id"],
                r["user_label"] or "",
                r["ts"].isoformat() if r["ts"] is not None else "",
                r["kind"],
                r["status"],
                r["duration_ms"] if r["duration_ms"] is not None else "",
                r["chars"] if r["chars"] is not None else "",
                r["bytes_in"] if r["bytes_in"] is not None else "",
                r["bytes_out"] if r["bytes_out"] is not None else "",
                r["error_class"] or "",
                r["request_id"] or "",
            ]
        )

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="wispralt-usage.csv"'},
    )
