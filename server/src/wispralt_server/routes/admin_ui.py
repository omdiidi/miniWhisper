"""
routes/admin_ui.py — Server-rendered Jinja2 admin UI under ``/admin/*``.

Two routers under the same prefix:

- :data:`public_router` exposes ``/admin/login`` (GET form + POST submit) —
  these endpoints MUST be reachable without auth, otherwise the operator
  has no way to acquire the session cookie that gates the rest of the UI.
- :data:`authed_router` covers everything else and is gated by both
  :func:`require_admin` and :func:`_require_db_pool`, so a browser hitting
  ``/admin/`` without a valid token is bounced to ``/admin/login``.

Auth model
----------
1. ``Authorization: Bearer ...`` — for curl / extension users.  Resolves
   via the same path as the dictation endpoints.
2. ``wispralt_admin_token`` cookie — for browser navigation; set by
   ``POST /admin/login`` with ``HttpOnly``, ``Secure``, ``SameSite=Strict``.
   ``_extract_bearer`` (in ``auth.py``) falls back to the cookie when the
   header is absent.

The login POST validates the token DIRECTLY against ``users.store.lookup``
+ the break-glass hash on app.state.  We do NOT call ``require_api_key``
recursively because Starlette's ``request.cookies`` is read-only — writing
into it has no effect on the very next dependency that would re-read it.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
import urllib.parse
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import auth as auth_mod
from ..auth import (
    require_admin,
    require_api_key,
    set_csrf_cookie,
    set_session_cookie,
    verify_csrf,
)
from ..config import settings
from ..constants import MAX_DISPLAY_NAME_LEN
from ..users import store as users_store
from ..users.store import User
from ..web.templates_env import templates

logger = logging.getLogger(__name__)


async def _require_db_pool(request: Request) -> asyncpg.Pool:
    """Dependency: 503 if the asyncpg pool is unavailable.

    The admin UI is unusable without Postgres (every page issues a query),
    so fail loudly rather than crashing on AttributeError further down.
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503, detail="Admin UI unavailable: Postgres degraded."
        )
    return pool


# Two routers under the same /admin prefix.
public_router = APIRouter(prefix="/admin")
authed_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin), Depends(_require_db_pool)],
)
# Self-only router: any authenticated user (admin OR employee) can hit /admin/me
# to see their own usage.  Gated by require_api_key (not require_admin) so
# employees don't get 403'd out of their own page.
me_router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(_require_db_pool)],
)


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
    """Validate *token* and set the session cookie on success.

    We resolve the token via the same paths the auth middleware uses
    (cache → Postgres → break-glass) but inline rather than calling
    :func:`require_api_key`: Starlette's ``request.cookies`` is read-only,
    so a recursive call to ``require_api_key`` after a hypothetical
    cookie-write would still see no cookie.
    """
    if not verify_csrf(request, csrf_token):
        return _render_admin_login_error(
            request,
            error="Form session expired. Please reload and try again.",
            status_code=403,
        )

    th = users_store.hash_token(token)
    pool = getattr(request.app.state, "db_pool", None)
    user: User | None = None

    # 1. Cache fast-path — if this admin token is already cached we can
    #    skip the DB round-trip.
    cached = auth_mod.token_cache.get(th)
    if cached is not None:
        user = cached

    # 2. Postgres lookup.
    if user is None and pool is not None:
        try:
            user = await users_store.lookup(pool, th)
        except (asyncpg.PostgresError, asyncpg.InterfaceError):
            # asyncpg has NO common base class for these two — PostgresError
            # (server-side) and InterfaceError ("pool is closed", client-side)
            # both inherit directly from Exception via separate private
            # hierarchies. The explicit tuple is the only correct form.
            # See 2026-05-17 postmortem.
            logger.exception("Postgres lookup failed during admin login")
            user = None
        if user is not None:
            auth_mod.token_cache.put(th, user)

    # 3. Break-glass: env-var hash always grants admin even when Postgres
    #    is degraded so the operator never gets locked out.
    if user is None:
        bg = getattr(request.app.state, "break_glass_token_hash", None)
        if bg is not None and th == bg:
            user = User(id=-1, label="break-glass-admin", role="admin")

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


# ── authed routes ─────────────────────────────────────────────────────────────


_OVERVIEW_SQL = """
WITH
  now_ts AS (SELECT now() AS t),
  totals AS (
    SELECT
      (SELECT count(*) FROM wispralt.users WHERE revoked_at IS NULL) AS active_users,
      (SELECT count(*) FROM wispralt.users) AS total_users,
      (SELECT count(*) FROM wispralt.usage_events
        WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS dictations_24h,
      (SELECT count(*) FROM wispralt.usage_events
        WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '7 days') AS dictations_7d,
      (SELECT count(*) FROM wispralt.usage_events
        WHERE ts >= (SELECT t FROM now_ts) - INTERVAL '30 days') AS dictations_30d,
      (SELECT count(*) FROM wispralt.usage_events
        WHERE status >= 400
          AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS errors_24h,
      (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
         FROM wispralt.usage_events
        WHERE duration_ms IS NOT NULL
          AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS p50_24h
  ),
  top_users AS (
    SELECT u.id, u.label, u.role, count(e.id) AS n
      FROM wispralt.users u
      LEFT JOIN wispralt.usage_events e
        ON e.user_id = u.id
       AND e.ts >= (SELECT t FROM now_ts) - INTERVAL '7 days'
     GROUP BY u.id, u.label, u.role
     ORDER BY n DESC
     LIMIT 5
  ),
  daily AS (
    SELECT to_char(date_trunc('day', e.ts), 'YYYY-MM-DD') AS day,
           count(*) AS n
      FROM wispralt.usage_events e
     WHERE e.ts >= (SELECT t FROM now_ts) - INTERVAL '14 days'
     GROUP BY 1
     ORDER BY 1
  )
SELECT
  (SELECT row_to_json(totals) FROM totals)::text AS totals,
  (SELECT json_agg(row_to_json(top_users)) FROM top_users)::text AS top_users,
  (SELECT json_agg(row_to_json(daily)) FROM daily)::text AS daily
"""


def _decode_json_col(raw: Any, default: Any) -> Any:
    """Decode a JSON-as-text column emitted by Postgres' ``::text`` cast.

    asyncpg returns the column as ``str`` (or ``None`` when the inner
    aggregate yielded no rows).  Decode to Python; fall back to *default*
    for ``None``.
    """
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    return json.loads(raw)


async def _aggregate_stats(pool: asyncpg.Pool) -> dict[str, Any]:
    """Single CTE round-trip serving every tile on the overview page."""
    row = await pool.fetchrow(_OVERVIEW_SQL)
    if row is None:
        return {"totals": {}, "top_users": [], "daily": []}
    return {
        "totals": _decode_json_col(row["totals"], {}),
        "top_users": _decode_json_col(row["top_users"], []),
        "daily": _decode_json_col(row["daily"], []),
    }


@authed_router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    pool = request.app.state.db_pool
    stats = await _aggregate_stats(pool)
    integration_count = await users_store.count_kind(pool, "integration")
    return templates.TemplateResponse(
        "overview.html.j2",
        {"request": request, "integration_count": integration_count, **stats},
    )


@authed_router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request) -> HTMLResponse:
    rows = await users_store.list_all(request.app.state.db_pool)
    return templates.TemplateResponse(
        "users.html.j2",
        {"request": request, "users": rows},
    )


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
    return (
        "curl -fsSL https://raw.githubusercontent.com/omdiidi/miniWhisper/main/install.sh \\\n"
        f"  | WISPRALT_API_KEY={plaintext} WISPRALT_SERVER={server_url} bash"
    )


@authed_router.get("/users/new", response_class=HTMLResponse)
async def users_add_form(request: Request) -> HTMLResponse:
    """Render the add-employee form."""
    return templates.TemplateResponse(
        "add_employee.html.j2",
        {
            "request": request,
            "form_label": "",
            "form_role": "employee",
            "form_display_name": "",
        },
    )


@authed_router.post("/users/new", response_class=HTMLResponse)
async def users_add_submit(
    request: Request,
    label: str = Form(...),
    role: str = Form("employee"),
    display_name: str = Form(""),
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
        return templates.TemplateResponse(
            "add_employee.html.j2",
            {
                "request": request,
                "error": err,
                "form_label": label,
                "form_role": role if role in _VALID_ROLES else "employee",
                "form_display_name": display_name,
            },
            status_code=400,
        )

    pool = request.app.state.db_pool
    user, plaintext = await users_store.mint(
        pool,
        label=clean_label,
        role=role,
        display_name=clean_display_name,
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
async def users_revoke(request: Request, user_id: int) -> RedirectResponse:
    """Revoke *user_id* and invalidate its cached token entry."""
    pool = request.app.state.db_pool
    revoked_hash = await users_store.revoke(pool, user_id)
    if revoked_hash:
        auth_mod.token_cache.invalidate(revoked_hash)
    return RedirectResponse("/admin/users", status_code=303)


@authed_router.post("/users/{user_id}/mint", response_class=HTMLResponse)
async def users_mint(request: Request, user_id: int) -> HTMLResponse:
    """Rotate the user's token in place; show the new plaintext once."""
    pool = request.app.state.db_pool
    new_plaintext, old_hash = await users_store.rotate(pool, user_id)
    if old_hash:
        auth_mod.token_cache.invalidate(old_hash)
    return templates.TemplateResponse(
        "token_minted.html.j2",
        {
            "request": request,
            "user_id": user_id,
            "plaintext": new_plaintext,
        },
    )


# ── /admin/keys — integration API keys (OpenAI-compat /v1 callers) ────────────
#
# Distinct from /admin/users (humans) — these are tokens issued for third-party
# programs (Buzz, MacWhisper, Open WebUI, etc.) that talk to /v1/audio/*.
# Under the hood: same wispralt.users row + role='employee', but with
# kind='integration' which both (a) hides them from /admin/users and
# (b) blocks them from /me/* and /telemetry/* via forbid_integration_kind.


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
    pool = request.app.state.db_pool
    rows = await users_store.list_integrations(pool)
    return templates.TemplateResponse(
        "keys.html.j2",
        {"request": request, "keys": rows},
    )


@authed_router.get("/keys/new", response_class=HTMLResponse)
async def keys_add_form(request: Request) -> HTMLResponse:
    """Render the add-integration-key form."""
    return templates.TemplateResponse(
        "add_key.html.j2",
        {"request": request, "form_program_name": ""},
    )


@authed_router.post("/keys/new", response_class=HTMLResponse)
async def keys_add_submit(
    request: Request,
    program_name: str = Form(...),
) -> HTMLResponse:
    """Mint an integration API key tied to a third-party program name.

    Steps:
        1. Validate ``program_name`` (1..MAX_DISPLAY_NAME_LEN chars, no
           control chars). Reuses ``_validate_optional_display_name`` but
           treats empty as an error here (Add Employee allows empty).
        2. Slugify the name into ``label = "key-<slug>"``.
        3. Mint the user row (role='employee', display_name=program_name).
        4. Flip ``kind`` to ``'integration'`` (separate UPDATE — column
           default is 'employee').
        5. Re-fetch the row so the User dataclass we pass to the template
           reflects the post-update ``kind='integration'`` value (avoids
           the stale-from-mint footgun).
        6. Render the key_added page with the plaintext + OpenAI env-var
           snippet.
    """
    clean_name, err = _validate_optional_display_name(program_name)
    if err is not None or not clean_name:
        return templates.TemplateResponse(
            "add_key.html.j2",
            {
                "request": request,
                "error": err or "Program name is required.",
                "form_program_name": program_name,
            },
            status_code=400,
        )

    slug = _slugify_program_name(clean_name)
    if not slug:
        return templates.TemplateResponse(
            "add_key.html.j2",
            {
                "request": request,
                "error": (
                    "Program name must contain at least one letter or digit."
                ),
                "form_program_name": program_name,
            },
            status_code=400,
        )

    label = f"{_KEY_LABEL_PREFIX}{slug}"

    pool = request.app.state.db_pool
    user, plaintext = await users_store.mint(
        pool,
        label=label,
        role="employee",
        display_name=clean_name,
    )
    await users_store.set_kind(pool, user_id=user.id, kind="integration")
    # Re-fetch so the value we hand to the template carries the updated
    # kind='integration'. The mint() return is built from the INSERT row,
    # which still shows the column-default 'employee'.
    refreshed = await users_store.lookup_by_id(pool, user.id)
    user_kind = refreshed.kind if refreshed is not None else "integration"

    return templates.TemplateResponse(
        "key_added.html.j2",
        {
            "request": request,
            "mode": "mint",
            "program_name": clean_name,
            "plaintext": plaintext,
            "label": label,
            "kind": user_kind,
        },
    )


@authed_router.post("/keys/{user_id}/revoke")
async def keys_revoke(request: Request, user_id: int) -> RedirectResponse:
    """Revoke an integration key and invalidate its cached token entry."""
    pool = request.app.state.db_pool
    revoked_hash = await users_store.revoke(pool, user_id)
    if revoked_hash:
        auth_mod.token_cache.invalidate(revoked_hash)
    return RedirectResponse("/admin/keys", status_code=303)


@authed_router.post("/keys/{user_id}/rotate", response_class=HTMLResponse)
async def keys_rotate(request: Request, user_id: int) -> HTMLResponse:
    """Rotate an integration key in place; show the new plaintext once."""
    pool = request.app.state.db_pool
    new_plaintext, old_hash = await users_store.rotate(pool, user_id)
    if old_hash:
        auth_mod.token_cache.invalidate(old_hash)

    # Re-fetch so we can show the original program name on the success page.
    refreshed = await users_store.lookup_by_id(pool, user_id)
    program_name = None
    label = None
    if refreshed is not None:
        label = refreshed.label
        # We don't have display_name on the cached User dataclass; pull it
        # via fetch_profile_by_id which returns it.
        profile = await users_store.fetch_profile_by_id(pool, user_id)
        if profile is not None:
            program_name = profile.display_name

    return templates.TemplateResponse(
        "key_added.html.j2",
        {
            "request": request,
            "mode": "rotate",
            "program_name": program_name,
            "plaintext": new_plaintext,
            "label": label,
            "kind": "integration",
        },
    )


_USER_DETAIL_SQL = """
WITH
  now_ts AS (SELECT now() AS t)
SELECT
  (SELECT count(*) FROM wispralt.usage_events
    WHERE user_id = $1
      AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS dictations_24h,
  (SELECT count(*) FROM wispralt.usage_events
    WHERE user_id = $1
      AND ts >= (SELECT t FROM now_ts) - INTERVAL '7 days') AS dictations_7d,
  (SELECT count(*) FROM wispralt.usage_events
    WHERE user_id = $1
      AND ts >= (SELECT t FROM now_ts) - INTERVAL '30 days') AS dictations_30d,
  (SELECT count(*) FROM wispralt.usage_events
    WHERE user_id = $1
      AND status >= 400
      AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS errors_24h,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
     FROM wispralt.usage_events
    WHERE user_id = $1
      AND duration_ms IS NOT NULL
      AND ts >= (SELECT t FROM now_ts) - INTERVAL '1 day') AS p50_24h
"""


async def _render_user_detail(request: Request, user_id: int) -> HTMLResponse:
    """Shared body for the admin /admin/users/{id} and self-only /admin/me routes."""
    pool = request.app.state.db_pool
    user = await users_store.fetch_profile_by_id(pool, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    totals_row = await pool.fetchrow(_USER_DETAIL_SQL, user_id)
    events = await pool.fetch(
        "SELECT ts, kind, status, duration_ms, bytes_in, error_class, request_id "
        "FROM wispralt.usage_events "
        "WHERE user_id = $1 ORDER BY ts DESC LIMIT 50",
        user_id,
    )
    return templates.TemplateResponse(
        "user_detail.html.j2",
        {
            "request": request,
            "user": user,
            "totals": dict(totals_row) if totals_row is not None else {},
            "events": [dict(e) for e in events],
        },
    )


@authed_router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int) -> HTMLResponse:
    return await _render_user_detail(request, user_id)


@me_router.get("/me", response_class=HTMLResponse)
async def me(request: Request, user: "User" = Depends(require_api_key)) -> Any:
    """Self-only landing page.

    Admins are redirected to the global dashboard so /admin/me is a safe
    universal entry point regardless of role — that lets the macOS client
    open ``<server>/admin/login`` without needing to know the role first.
    Employees see their own user_detail page (24h/7d/30d tiles + last 50
    events) with the admin nav links hidden by base.html.j2.
    """
    if user.role == "admin":
        return RedirectResponse("/admin/", status_code=303)
    if user.id < 0:
        # Sentinel break-glass user without an admin role shouldn't reach
        # here in normal operation, but guard the case to avoid a 500.
        raise HTTPException(status_code=403, detail="Account not provisioned")
    return await _render_user_detail(request, user.id)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _build_usage_query(
    pool_query: dict[str, Any], where: list[str], params: list[Any]
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause for /usage filters."""
    if pool_query.get("kind"):
        params.append(pool_query["kind"])
        where.append(f"e.kind = ${len(params)}")
    if pool_query.get("status") is not None:
        params.append(pool_query["status"])
        where.append(f"e.status = ${len(params)}")
    if pool_query.get("user_id") is not None:
        params.append(pool_query["user_id"])
        where.append(f"e.user_id = ${len(params)}")
    if pool_query.get("since"):
        params.append(pool_query["since"])
        where.append(f"e.ts >= ${len(params)}")
    if pool_query.get("until"):
        params.append(pool_query["until"])
        where.append(f"e.ts < ${len(params)}")
    return (" AND ".join(where) if where else "TRUE"), params


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
    pool = request.app.state.db_pool
    filters_in = {
        "kind": kind,
        "status": status,
        "user_id": user_id,
        "since": _parse_dt(since),
        "until": _parse_dt(until),
    }
    where_sql, params = _build_usage_query(filters_in, [], [])
    params_with_paging = list(params) + [_PAGE_SIZE + 1, offset]
    sql = (
        "SELECT e.id, e.user_id, u.label AS user_label, e.ts, e.kind, e.status, "
        "       e.duration_ms, e.bytes_in, e.error_class, e.request_id "
        "  FROM wispralt.usage_events e "
        "  LEFT JOIN wispralt.users u ON u.id = e.user_id "
        f" WHERE {where_sql} "
        " ORDER BY e.ts DESC "
        f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    )
    rows = await pool.fetch(sql, *params_with_paging)
    has_more = len(rows) > _PAGE_SIZE
    page_rows = [dict(r) for r in rows[:_PAGE_SIZE]]

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
    pool = request.app.state.db_pool
    filters_in = {
        "kind": kind,
        "status": status,
        "user_id": user_id,
        "since": _parse_dt(since),
        "until": _parse_dt(until),
    }
    where_sql, params = _build_usage_query(filters_in, [], [])
    sql = (
        "SELECT e.id, e.user_id, u.label AS user_label, e.ts, e.kind, e.status, "
        "       e.duration_ms, e.chars, e.bytes_in, e.bytes_out, e.error_class, "
        "       e.request_id "
        "  FROM wispralt.usage_events e "
        "  LEFT JOIN wispralt.users u ON u.id = e.user_id "
        f" WHERE {where_sql} "
        " ORDER BY e.ts DESC "
        " LIMIT 10000"
    )
    rows = await pool.fetch(sql, *params)

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
