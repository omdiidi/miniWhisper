"""
routes/admin_invites.py — invites: the admin pages and the public redeem endpoint.

* ``router`` (admin): ``GET /admin/invites`` (list + create form), ``POST /admin/invites``
  (new person, or "add device" with ``user_id``; shows the join link and token once,
  ``Cache-Control: no-store``) and ``POST /admin/invites/{id}/revoke``. Mutations take
  ``require_admin_csrf``.
* ``public_router``: ``POST /invites/redeem`` (JSON ``{invite, device_label?}``) →
  ``{token, user_label, host}``. NOT under the admin dependency. Every failure, including
  a malformed body, is the same ``400 {"error": "invite_invalid"}``; more than 10 attempts
  a minute from one client IP → 429. The client IP is ``CF-Connecting-IP`` when present
  (cloudflared), else the socket peer.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .._errors import InviteInvalid
from ..accounts import service as account_service
from ..auth import require_admin, require_admin_csrf, set_csrf_cookie
from ..config import settings
from ..web.templates_env import templates
from .admin_ui import NO_STORE, _validate_label

if TYPE_CHECKING:
    from ..accounts.store import AccountStore, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
public_router = APIRouter()

REDEEM_PER_MIN = 10
_WINDOW_S = 60.0
_DEVICE_LABEL_MAX = 80
_INVALID = {"error": "invite_invalid"}


def _accounts(request: Request) -> AccountStore:
    return request.app.state.accounts


async def _page(request: Request, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    invites = await asyncio.to_thread(_accounts(request).list_invites, time.time())
    csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        "invites.html.j2",
        {"request": request, "invites": invites, "csrf_token": csrf, "error": error},
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf, request)
    return response


@router.get("/invites", response_class=HTMLResponse)
async def invites_list(request: Request) -> HTMLResponse:
    return await _page(request)


@router.post("/invites", response_class=HTMLResponse)
async def invites_create(
    request: Request,
    label: str = Form(""),
    role: str = Form("employee"),
    user_id: int | None = Form(None),
    admin: User = Depends(require_admin_csrf),
) -> HTMLResponse:
    """New person (``label`` + ``role``) or "add device" for an existing ``user_id``."""
    accounts = _accounts(request)
    clean_label: str | None = None
    if user_id is None:
        clean_label, err = _validate_label(label)
        if err is None and role not in ("admin", "employee"):
            err = "Invalid role."
        if err is not None:
            return await _page(request, error=err, status_code=400)
    else:
        role = "employee"  # ignored for "add device": the person keeps their role
    try:
        _id, token, expires_at = await asyncio.to_thread(
            lambda: account_service.create_invite(
                accounts, created_by=admin.id, user_id=user_id, label=clean_label, role=role
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    target = await asyncio.to_thread(accounts.profile, user_id) if user_id is not None else None
    return templates.TemplateResponse(
        "invite_created.html.j2",
        {
            "request": request,
            "link": account_service.join_link(settings.server_url, token),
            "token": token,
            "label": target.label if target else clean_label,
            "add_device": user_id is not None,
            "user_id": user_id,
            "ttl_min": account_service.INVITE_TTL_S // 60,
            "server_url": settings.server_url,
        },
        headers=NO_STORE,
    )


@router.post("/invites/{invite_id}/revoke")
async def invites_revoke(
    request: Request, invite_id: int, _admin: User = Depends(require_admin_csrf)
) -> RedirectResponse:
    await asyncio.to_thread(_accounts(request).revoke_invite, invite_id, time.time())
    return RedirectResponse("/admin/invites", status_code=303)


# ── public redeem ────────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip", "").strip() or (
        request.client.host if request.client else "unknown"
    )


def _over_limit(request: Request, now: float) -> bool:
    """Sliding 60 s window per client IP on ``app.state`` (no awaits: no lock needed)."""
    buckets: dict[str, collections.deque[float]] | None = getattr(
        request.app.state, "invite_redeem_buckets", None
    )
    if buckets is None:
        buckets = {}
        request.app.state.invite_redeem_buckets = buckets
    q = buckets.setdefault(_client_ip(request), collections.deque())
    while q and q[0] <= now - _WINDOW_S:
        q.popleft()
    if len(q) >= REDEEM_PER_MIN:
        return True
    q.append(now)
    return False


def _parse(raw: bytes) -> tuple[str, str | None] | None:
    try:
        body: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    invite = body.get("invite")
    device = body.get("device_label")
    if not isinstance(invite, str) or not 0 < len(invite) <= 200:
        return None
    if not isinstance(device, str) or any(ord(c) < 0x20 or ord(c) == 0x7F for c in device):
        device = None
    else:
        device = device.strip()[:_DEVICE_LABEL_MAX] or None
    return invite, device


@public_router.post("/invites/redeem")
async def invites_redeem(request: Request) -> JSONResponse:
    if _over_limit(request, time.monotonic()):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    parsed = _parse(await request.body())
    if parsed is None:
        return JSONResponse(_INVALID, status_code=400)
    invite, device_label = parsed
    try:
        user, token = await asyncio.to_thread(
            account_service.redeem_invite, _accounts(request), invite, device_label
        )
    except InviteInvalid:
        return JSONResponse(_INVALID, status_code=400)
    return JSONResponse(
        {"token": token, "user_label": user.label, "host": settings.server_url},
        headers=NO_STORE,
    )
