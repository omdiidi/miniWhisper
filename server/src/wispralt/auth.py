"""
auth.py — bearer-token authentication against the local SQLite DB.

Path of an inbound token:

    1. Extract it from ``Authorization: Bearer ...`` (or, for browser pages,
       the ``wispralt_admin_token`` cookie set by ``/admin/login`` / ``/me/login``).
    2. ``sha256`` the plaintext.
    3. ``AccountStore.lookup_key`` — one indexed ``api_keys ⋈ users`` read on
       **every** request (no cache), so a revoke, disable or rotate, in this
       process or from another one, applies to the next request.
    4. No row → 401. ``sqlite3.Error`` → 503. There is no fallback admin.

The ``User`` is stashed on ``request.state.user`` (the usage middleware reads
it) and ``request.state.auth_via`` records ``"bearer"`` or ``"cookie"``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import sqlite3
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from wispralt.accounts.service import hash_token
from wispralt.accounts.store import User

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_bearer(request: Request) -> str | None:
    """Extract a bearer token from the ``Authorization`` header ONLY.

    Cookie fallback is intentionally NOT consulted here — callers that
    accept session-cookie auth (the admin UI surface via
    :func:`require_api_key`) layer the cookie lookup on top. Callers that
    must be Bearer-only (the OpenAI-compat /v1 surface via
    :func:`require_bearer`) get the right behavior for free by using
    this helper directly.

    Raises
    ------
    HTTPException(400)
        Multiple ``Authorization`` headers are sent (ambiguous intent).
    """
    headers = request.headers.getlist("authorization")
    if len(headers) > 1:
        raise HTTPException(status_code=400, detail="Multiple Authorization headers not allowed")
    if not headers:
        return None
    raw = headers[0]
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[len("bearer "):].strip()
    return token or None


# ── token resolution ──────────────────────────────────────────────────────────


async def resolve_token(app: FastAPI, plaintext: str) -> User | None:
    """The enabled user owning *plaintext*, else None. 503 on ``sqlite3.Error``.

    Shared by the auth dependencies and the two login forms.
    """
    accounts = getattr(app.state, "accounts", None)
    if accounts is None:
        raise HTTPException(status_code=503, detail="Auth temporarily unavailable")
    try:
        return await asyncio.to_thread(accounts.lookup_key, hash_token(plaintext))
    except sqlite3.Error as exc:
        logger.error("auth lookup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Auth temporarily unavailable") from exc


async def _resolve_token_user(request: Request, plaintext: str, via: str) -> User:
    user = await resolve_token(request.app, plaintext)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    request.state.user = user
    request.state.auth_via = via
    return user


# ── FastAPI dependencies ──────────────────────────────────────────────────────


async def require_api_key(request: Request) -> User:
    """Authenticate the request and return the resolved :class:`User`.

    Accepts EITHER an ``Authorization: Bearer ...`` header OR the
    ``wispralt_admin_token`` cookie (used by the admin UI's browser
    session flow — cross-page navigations can't carry Authorization
    headers).

    Raises 400 on multiple ``Authorization`` headers, 401 on a missing or
    invalid token, 503 when the DB lookup fails.
    """
    bearer = _extract_bearer(request)
    via = "bearer"
    if not bearer:
        # Cookie fallback: admin UI browser navigations can't attach
        # Authorization headers, so /admin/login sets ``wispralt_admin_token``
        # as a session cookie and we accept it here. Bearer-only surfaces
        # (/v1/*, /transcribe/*) use :func:`require_bearer` which deliberately
        # skips this fallback; cookie mutations take :func:`require_csrf`.
        bearer = request.cookies.get(SESSION_COOKIE_NAME) or None
        via = "cookie"
    if not bearer:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    return await _resolve_token_user(request, bearer, via)


async def require_bearer(request: Request) -> User:
    """Bearer required, cookie ignored: ``/v1/*`` and ``/transcribe/*``.

    These are API surfaces (the Mac client, OpenAI SDKs) that always send
    ``Authorization: Bearer``. Refusing the session cookie means a browser
    carrying the portal cookie can never drive them, so they need no CSRF
    token (the portal itself never calls them).
    """
    plaintext = _extract_bearer(request)
    if not plaintext:
        raise HTTPException(
            status_code=401,
            detail="Expected 'Bearer <token>' Authorization header",
        )
    return await _resolve_token_user(request, plaintext, "bearer")


async def forbid_integration_kind(
    user: User = Depends(require_api_key),
) -> User:
    """Dep for /me/* — rejects integration-kind tokens.

    Integration keys (``kind='integration'``) are minted for third-party
    programs talking to /v1/audio/transcriptions. They should NOT have
    access to dictation history or other per-user data — those are
    human-only surfaces.

    NOTE: do NOT chain this after :func:`require_bearer` — /v1 is
    precisely the surface that integration keys are FOR.
    """
    if user.kind == "integration":
        raise HTTPException(
            status_code=403,
            detail=(
                "Integration keys cannot access /me — "
                "use /v1/audio/transcriptions only"
            ),
        )
    return user


def require_admin(user: User = Depends(require_api_key)) -> User:
    """Variant of :func:`require_api_key` that also requires ``role='admin'``."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


CSRF_HEADER = "X-CSRF-Token"


async def require_csrf(request: Request, user: User = Depends(require_api_key)) -> User:
    """Auth, then CSRF for a mutation. Use as a PARAMETER dependency.

    Auth is this dependency's own sub-dependency, so it always resolves first
    and sets ``request.state.auth_via`` (a decorator-level ``dependencies=[…]``
    entry would run before parameter dependencies). The token is skipped
    **only** when that auth used a valid Bearer header: a non-Bearer
    ``Authorization`` (e.g. ``Basic``) falls back to the cookie, so it must
    carry the token like any cookie request. Otherwise the ``X-CSRF-Token``
    header or the ``csrf_token`` form field must equal the CSRF cookie, else 403.
    """
    if getattr(request.state, "auth_via", None) == "bearer":
        return user
    token = request.headers.get(CSRF_HEADER)
    if token is None:
        value = (await request.form()).get("csrf_token")  # cached; route Form() params still work
        token = value if isinstance(value, str) else ""
    if not verify_csrf(request, token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return user


def require_admin_csrf(user: User = Depends(require_csrf)) -> User:
    """:func:`require_csrf` + ``role='admin'`` (the admin portal's mutations)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


# ── shared session-cookie + CSRF helpers ──────────────────────────────────────
#
# Both /admin/login (admin_ui.py) and /me/login (routes/me.py) issue the same
# ``wispralt_admin_token`` cookie. Centralizing here means flag changes
# (max_age, samesite, etc.) hit both surfaces and the CSRF double-submit
# scheme stays consistent across forms.

SESSION_COOKIE_NAME = "wispralt_admin_token"
CSRF_COOKIE_NAME = "wispralt_csrf"
_SESSION_COOKIE_MAX_AGE_S = 8 * 3600  # 8h
_CSRF_COOKIE_MAX_AGE_S = 30 * 60  # 30m — forms shouldn't sit open longer
_LEGACY_PATHS = ("/admin/login",)  # historical paths to clear on new login


def set_session_cookie(resp: Response, token: str) -> None:
    """Set the unified session cookie and clear any legacy-path cookies."""
    for legacy_path in _LEGACY_PATHS:
        resp.delete_cookie(SESSION_COOKIE_NAME, path=legacy_path)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=_SESSION_COOKIE_MAX_AGE_S,
        path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    """Clear the session cookie (used on logout, if ever added)."""
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    for legacy_path in _LEGACY_PATHS:
        resp.delete_cookie(SESSION_COOKIE_NAME, path=legacy_path)


def set_csrf_cookie(resp: Response, csrf_token: str) -> None:
    """Set the CSRF double-submit cookie.

    NOT HttpOnly: the form template echoes the value back as a hidden input,
    and the POST handler compares cookie vs form via ``hmac.compare_digest``.
    SameSite=strict so a cross-origin POST can't carry the cookie alone.
    """
    resp.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        max_age=_CSRF_COOKIE_MAX_AGE_S,
        path="/",
    )


def csrf_token_for(request: Request) -> str:
    """The CSRF cookie's current token, else a new one.

    For partials rendered into a page that already carries forms: rotating
    the cookie there would invalidate the tokens those forms hold.
    """
    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)


def verify_csrf(request: Request, form_token: str) -> bool:
    """Constant-time compare of the form token vs the CSRF cookie.

    Returns False (rather than raising) so the caller can render a friendly
    "form expired, please reload" page in the same template as other login
    errors. Empty / missing cookie returns False — no implicit pass-through.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)
