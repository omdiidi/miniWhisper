"""
auth.py — Multi-token bearer authentication backed by Supabase Postgres.

Path of an inbound bearer token:

    1. Extract from ``Authorization: Bearer ...`` header (or, for the admin
       UI, the ``wispralt_admin_token`` cookie set by ``/admin/login``).
    2. ``sha256`` the plaintext.
    3. Look up the hash in the in-process :class:`TokenCache` (60 s TTL).
    4. Cache miss → query ``wispralt.users`` via the asyncpg pool stashed
       on ``app.state.db_pool``.
    5. Postgres unreachable / row missing → fall through to the
       break-glass admin path: a single env-var hash stashed on
       ``app.state.break_glass_token_hash`` grants admin so the operator
       cannot get locked out of the server they administer.

The ``User`` returned by :func:`require_api_key` is also stashed on
``request.state.user`` so the observability middleware can attribute
usage events.

Legacy compatibility: ``current_key()`` and ``set_current_key()`` are
retained as thin shims so the existing ``routes/admin.py:rotate-key``
endpoint can keep operating on the env-var token until the admin UI's
mint/revoke flow lands in Phase B/C.
"""

from __future__ import annotations

import hmac
import logging
import threading

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from wispralt_server.users import cache as _cache_mod
from wispralt_server.users import store as _store_mod
from wispralt_server.users.store import User

logger = logging.getLogger(__name__)


# ── singletons ────────────────────────────────────────────────────────────────

# In-process token cache shared across all requests.  Exported so callers
# (e.g. ``routes/admin_ui.py``) can ``invalidate`` after revoke / rotate.
token_cache = _cache_mod.TokenCache()


# ── legacy single-key shim ────────────────────────────────────────────────────
#
# The break-glass admin path sources its hash from
# ``app.state.break_glass_token_hash`` (set by lifespan in ``main.py``).
# These shims survive only so the existing rotate-key endpoint and any
# tests that import ``current_key`` keep working until Phase B replaces
# them with the admin UI's mint/rotate flow.

_lock = threading.Lock()
_current_key: str | None = None


def _load_key_from_env() -> str:
    """Read the API key from the environment via settings.

    Falls back to the ``WISPRALT_API_KEY`` env var directly if
    pydantic-settings fails (e.g. minimal test environments).
    """
    try:
        from wispralt_server.config import settings

        return settings.wispralt_api_key.get_secret_value()
    except (ImportError, ValueError, RuntimeError):
        import os

        key = os.environ.get("WISPRALT_API_KEY", "")
        if not key:
            raise RuntimeError(
                "WISPRALT_API_KEY is not set. "
                "Ensure the .env file exists or the environment variable is exported."
            ) from None
        return key


def current_key() -> str:
    """Return the current break-glass API key.  Lazy + thread-safe."""
    global _current_key
    with _lock:
        if _current_key is None:
            _current_key = _load_key_from_env()
        return _current_key


def set_current_key(new_key: str) -> None:
    """Replace the in-memory break-glass key.  Called by ``rotate-key``."""
    global _current_key
    with _lock:
        _current_key = new_key


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_bearer(request: Request) -> str | None:
    """Extract a bearer token from the ``Authorization`` header ONLY.

    Cookie fallback is intentionally NOT consulted here — callers that
    accept session-cookie auth (the admin UI surface via
    :func:`require_api_key`) layer the cookie lookup on top. Callers that
    must be Bearer-only (the OpenAI-compat /v1 surface via
    :func:`require_api_key_v1`) get the right behavior for free by using
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


# ── shared token-resolution state machine ─────────────────────────────────────


async def _resolve_token_user(request: Request, plaintext: str) -> User:
    """Resolve a plaintext bearer token to a :class:`User`.

    Shared by :func:`require_api_key` (Bearer + cookie) and
    :func:`require_api_key_v1` (Bearer-only). Side-effect: sets
    ``request.state.user`` on every success path so the observability
    middleware can attribute usage events.

    State machine (preserve EXACTLY — security-critical):
        1. Cache fast-path: ``token_cache.get(th)`` hit → stash on
           ``request.state.user`` and return.
        2. Postgres lookup via ``users_store.lookup(pool, th)``:
           2a. Row found → cache it, stash on request.state.user, return.
           2b. Row not found AND Postgres did NOT error → 401. (Revocation
               invariant: do NOT consult break-glass — that would
               undermine revoking a compromised token while Postgres is
               healthy.)
        3. Postgres errored (caught ``(asyncpg.PostgresError,
           asyncpg.InterfaceError)`` — see 2026-05-17 postmortem for why
           this exact tuple, no common base class) OR pool is None:
           3a. Break-glass hash matches via ``hmac.compare_digest`` →
               return the synthetic admin sentinel
               ``User(id=-1, label="break-glass-admin", role="admin",
               kind="employee")``. id=-1 tells the observability
               middleware to skip the usage-event enqueue (no FK
               violation against ``wispralt.usage_events.user_id``).
           3b. No break-glass match → 503 so legitimate clients see
               "service degraded" (NOT 401 — that would falsely suggest
               their token is wrong).
    """
    th = _store_mod.hash_token(plaintext)

    # 1. cache fast-path
    cached = token_cache.get(th)
    if cached is not None:
        request.state.user = cached
        return cached

    # 2. Postgres lookup. The seeded break-glass row also lives here once
    #    the lifespan has run; this is the path 99% of break-glass calls
    #    take. Track WHY user is None — "row missing" (token revoked or
    #    invalid) must NOT fall through to break-glass; only "Postgres
    #    errored" or "pool unavailable" should trigger that escape hatch.
    pool = getattr(request.app.state, "db_pool", None)
    postgres_errored = False
    if pool is not None:
        try:
            user = await _store_mod.lookup(pool, th)
        except (asyncpg.PostgresError, asyncpg.InterfaceError):
            # asyncpg has NO common base class for these two — PostgresError
            # (server-side) and InterfaceError ("pool is closed", client-side)
            # both inherit directly from Exception via separate private
            # hierarchies. The explicit tuple is the only correct form.
            # See 2026-05-17 postmortem.
            logger.exception("Postgres lookup failed; falling through to break-glass")
            user = None
            postgres_errored = True
        if user is not None:
            token_cache.put(th, user)
            request.state.user = user
            return user
        if not postgres_errored:
            # Branch 2b: Postgres said "no row" — token is invalid OR
            # was revoked. Do NOT consult break-glass: that would defeat
            # revocation.
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    # 3. Pool is None OR Postgres errored. The env-var token grants admin
    #    so the operator never locks themselves out. Constant-time compare
    #    on the hex digest to avoid timing oracles. user.id = -1 is a
    #    sentinel; the observability middleware skips usage-event enqueue
    #    for negative ids (no FK violation against wispralt.usage_events).
    bg_hash = getattr(request.app.state, "break_glass_token_hash", None)
    if bg_hash is not None and hmac.compare_digest(th, bg_hash):
        user = User(
            id=-1,
            label="break-glass-admin",
            role="admin",
            kind="employee",
        )
        request.state.user = user
        logger.warning("auth: break-glass admin path used (Postgres degraded)")
        return user

    # No pool, no break-glass match → 503 so legitimate clients know
    # the service is degraded (NOT 401, which would suggest the token
    # is wrong).
    raise HTTPException(status_code=503, detail="Auth temporarily unavailable")


# ── FastAPI dependencies ──────────────────────────────────────────────────────


async def require_api_key(request: Request) -> User:
    """Authenticate the request and return the resolved :class:`User`.

    Accepts EITHER an ``Authorization: Bearer ...`` header OR the
    ``wispralt_admin_token`` cookie (used by the admin UI's browser
    session flow — cross-page navigations can't carry Authorization
    headers).

    Side effects:
        - Stores the user on ``request.state.user`` for the observability
          middleware to attribute usage events (via
          :func:`_resolve_token_user`).

    Raises
    ------
    HTTPException(400)
        Multiple ``Authorization`` headers (via :func:`_extract_bearer`).
    HTTPException(401)
        Missing / empty / invalid bearer token.
    HTTPException(503)
        Postgres pool unavailable AND the bearer doesn't match the
        break-glass hash — operator can't recover this without fixing
        Postgres or re-setting WISPRALT_API_KEY in the env.
    """
    bearer = _extract_bearer(request)
    if not bearer:
        # Cookie fallback: admin UI browser navigations can't attach
        # Authorization headers, so /admin/login sets ``wispralt_admin_token``
        # as a session cookie and we accept it here. Bearer-only surfaces
        # (i.e. /v1/*) use :func:`require_api_key_v1` which deliberately
        # skips this fallback.
        bearer = request.cookies.get("wispralt_admin_token") or None
    if not bearer:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    return await _resolve_token_user(request, bearer)


async def require_api_key_v1(request: Request) -> User:
    """/v1/* only: Bearer required, cookie ignored.

    Third-party API surface — programs sending the admin session cookie
    by accident should NOT be auth'd through this dep. Browser-based
    OpenAI clients send Bearer too (the OpenAI SDK sets the
    ``Authorization`` header explicitly), so we lose nothing by refusing
    the cookie path here.
    """
    plaintext = _extract_bearer(request)
    if not plaintext:
        raise HTTPException(
            status_code=401,
            detail="Expected 'Bearer <token>' Authorization header",
        )
    return await _resolve_token_user(request, plaintext)


async def forbid_integration_kind(
    user: User = Depends(require_api_key),
) -> User:
    """Dep for /me/* and /telemetry/* — rejects integration-kind tokens.

    Integration keys (``kind='integration'``) are minted for third-party
    programs talking to /v1/audio/transcriptions. They should NOT have
    access to dictation history or user-event telemetry — those are
    human-only surfaces.

    NOTE: do NOT chain this after :func:`require_api_key_v1` — /v1 is
    precisely the surface that integration keys are FOR.
    """
    if user.kind == "integration":
        raise HTTPException(
            status_code=403,
            detail=(
                "Integration keys cannot access /me or /telemetry — "
                "use /v1/audio/transcriptions only"
            ),
        )
    return user


def require_admin(user: User = Depends(require_api_key)) -> User:
    """Variant of :func:`require_api_key` that also requires ``role='admin'``."""
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
