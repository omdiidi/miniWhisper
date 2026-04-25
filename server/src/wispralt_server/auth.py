"""
auth.py — Bearer-token authentication for WisprAlt.

The current API key lives in a module-level string guarded by a threading.Lock
so the key-rotation endpoint can hot-swap it without a server restart.

Usage (FastAPI dependency)::

    from fastapi import Depends
    from wispralt_server.auth import require_api_key

    @router.post("/some/endpoint", dependencies=[Depends(require_api_key)])
    async def my_handler(): ...
"""

from __future__ import annotations

import secrets
import threading

from fastapi import HTTPException, Request

# ── in-memory key storage ─────────────────────────────────────────────────────
#
# _current_key is populated lazily on first call to current_key() so that
# importing this module without a .env present (e.g. unit-test environments)
# does not crash at import time.  The actual settings object is still imported
# at module level for use by the hot-swap path, but we defer the secret read.

_lock = threading.Lock()
_current_key: str | None = None


def _load_key_from_env() -> str:
    """Read the API key from the environment via settings.

    Called once (lazily) by current_key().  Falls back to the WISPRALT_API_KEY
    environment variable directly if pydantic-settings fails (e.g. missing
    other required env vars in a minimal test environment).
    """
    try:
        from wispralt_server.config import settings  # local import to defer validation
        return settings.wispralt_api_key.get_secret_value()
    except Exception:  # noqa: BLE001 — best-effort fallback
        import os
        key = os.environ.get("WISPRALT_API_KEY", "")
        if not key:
            raise RuntimeError(
                "WISPRALT_API_KEY is not set. "
                "Ensure the .env file exists or the environment variable is exported."
            )
        return key


def current_key() -> str:
    """Return the current API key.  Thread-safe read; lazy initialisation."""
    global _current_key
    with _lock:
        if _current_key is None:
            _current_key = _load_key_from_env()
        return _current_key


def set_current_key(new_key: str) -> None:
    """Replace the in-memory API key.  Thread-safe write.

    Called by ``POST /admin/rotate-key`` after the new key has been persisted
    to .env so in-memory and on-disk are always in sync.
    """
    global _current_key
    with _lock:
        _current_key = new_key


# ── FastAPI dependency ────────────────────────────────────────────────────────


def require_api_key(request: Request) -> None:
    """FastAPI dependency that raises HTTP 401 on missing or wrong bearer token.

    Uses ``secrets.compare_digest`` to prevent timing-oracle attacks.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    provided = authorization[len("bearer "):].strip()
    if not secrets.compare_digest(provided, current_key()):
        raise HTTPException(status_code=401, detail="Invalid token")
