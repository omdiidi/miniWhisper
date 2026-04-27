"""JSON /me endpoint for client identity self-management.

Auth: any valid Bearer token (admin or employee). Each user can only read or write
their own row — there is no path parameter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from wispralt_server.auth import require_api_key
from wispralt_server.constants import MAX_DISPLAY_NAME_LEN
from wispralt_server.users import store as users_store
from wispralt_server.users.store import User

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
