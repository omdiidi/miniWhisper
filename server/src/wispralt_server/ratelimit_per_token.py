"""
ratelimit_per_token.py — Post-auth per-token rate limit dependency for /v1.

Why this lives here (not in middleware/rate_limit.py): middleware sees the
raw Request before any auth dep resolves, so it can't bucket by user.id.
This module exposes a FastAPI Depends that runs AFTER require_api_key_v1
resolves the User, then maintains a per-user-id deque.

Buckets live on app.state.v1_rate_buckets (NOT a module-level dict — test
isolation requires per-app state).

Limit: 60 req / 60s window per user.id.
Break-glass users (user.id < 0) bypass — no key id to bucket on.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

from fastapi import Depends, HTTPException, Request

from wispralt_server.auth import require_api_key_v1
from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

_PER_MIN = 60
_WINDOW_S = 60.0


def init_rate_limit_state(app) -> None:
    """Initialize rate-limit state on app.state. Call from main.py lifespan startup
    BEFORE yield. Idempotent — safe to call multiple times."""
    if not hasattr(app.state, "v1_rate_buckets"):
        app.state.v1_rate_buckets = {}
    if not hasattr(app.state, "v1_rate_buckets_lock"):
        app.state.v1_rate_buckets_lock = asyncio.Lock()


async def rate_limit_v1_per_token(
    request: Request,
    user: User = Depends(require_api_key_v1),
) -> User:
    """Enforce 60 req / 60s per user.id.

    Returns the User so the route can use this as the single auth dep
    (FastAPI's dependency cache resolves `require_api_key_v1` exactly once
    per request via this transitive chain).

    Defensive lazy-init: if lifespan didn't run (older TestClient usage
    without `with`), initialize on first miss and warn.
    """
    if user.id < 0:
        return user  # break-glass exemption

    buckets = getattr(request.app.state, "v1_rate_buckets", None)
    lock = getattr(request.app.state, "v1_rate_buckets_lock", None)
    if buckets is None or lock is None:
        logger.warning(
            "v1 rate-limit state missing from app.state — lazy-initializing "
            "(use `with TestClient(app):` to trigger lifespan startup)"
        )
        init_rate_limit_state(request.app)
        buckets = request.app.state.v1_rate_buckets
        lock = request.app.state.v1_rate_buckets_lock

    now = time.monotonic()
    async with lock:
        bucket = buckets.get(user.id)
        if bucket is None:
            bucket = collections.deque()
            buckets[user.id] = bucket
        # Sweep stale entries (older than window) — amortized O(1)
        while bucket and now - bucket[0] > _WINDOW_S:
            bucket.popleft()
        if len(bucket) >= _PER_MIN:
            retry_after = max(1, int(_WINDOW_S - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded: {_PER_MIN} requests per minute per token"
                ),
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
    return user
