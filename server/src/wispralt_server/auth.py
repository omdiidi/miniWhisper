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

import logging
import threading

import asyncpg
from fastapi import Depends, HTTPException, Request

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
    """Extract a bearer token from the request.

    Order of precedence:
        1. ``Authorization: Bearer ...`` header.
        2. ``wispralt_admin_token`` cookie (used by the admin UI's browser
           session flow — Authorization headers can't be attached to
           cross-page navigations).

    Raises
    ------
    HTTPException(400)
        Multiple ``Authorization`` headers are sent (ambiguous intent).
    """
    headers = request.headers.getlist("authorization")
    if len(headers) > 1:
        raise HTTPException(status_code=400, detail="Multiple Authorization headers not allowed")
    if not headers:
        cookie = request.cookies.get("wispralt_admin_token")
        return cookie or None
    raw = headers[0]
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[len("bearer "):].strip()
    return token or None


# ── FastAPI dependencies ──────────────────────────────────────────────────────


async def require_api_key(request: Request) -> User:
    """Authenticate the request and return the resolved :class:`User`.

    Side effects:
        - Stores the user on ``request.state.user`` for the observability
          middleware to attribute usage events.

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
        raise HTTPException(status_code=401, detail="Missing bearer token")

    th = _store_mod.hash_token(bearer)

    # 1. cache fast-path
    cached = token_cache.get(th)
    if cached is not None:
        request.state.user = cached
        return cached

    # 2. Postgres lookup.  The seeded break-glass row also lives here once
    #    the lifespan has run; this is the path 99% of break-glass calls
    #    take.
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        try:
            user = await _store_mod.lookup(pool, th)
        except asyncpg.PostgresError:
            logger.exception("Postgres lookup failed; trying break-glass")
            user = None
        if user is not None:
            token_cache.put(th, user)
            request.state.user = user
            return user

    # 3. Break-glass: pool is None OR Postgres errored OR row missing.
    #    Env-var token still grants admin so the operator never locks
    #    themselves out.  user.id = -1 is a sentinel; the observability
    #    middleware skips usage-event enqueue for negative ids (no FK
    #    violation against wispralt.usage_events).
    bg_hash = getattr(request.app.state, "break_glass_token_hash", None)
    if bg_hash is not None and th == bg_hash:
        user = User(id=-1, label="break-glass-admin", role="admin")
        request.state.user = user
        logger.warning("auth: break-glass admin path used (Postgres degraded)")
        return user

    if pool is None:
        raise HTTPException(status_code=503, detail="Auth temporarily unavailable")
    raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_admin(user: User = Depends(require_api_key)) -> User:
    """Variant of :func:`require_api_key` that also requires ``role='admin'``."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
