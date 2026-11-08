"""
accounts/service.py — the only place plaintext tokens exist.

Each function generates a 64-hex token, hands only its sha256 to
``AccountStore`` and returns the plaintext to the caller, who shows it to the
operator exactly once. Nothing here logs a token or its hash.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from wispralt.accounts.store import AccountStore, User

logger = logging.getLogger(__name__)


def hash_token(plaintext: str) -> str:
    """sha256 hex digest of *plaintext* (what ``api_keys.token_hash`` stores)."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _new_token() -> tuple[str, str]:
    plaintext = secrets.token_hex(32)
    return plaintext, hash_token(plaintext)


def create_user(
    accounts: AccountStore,
    *,
    label: str,
    role: str,
    kind: str,
    display_name: str | None = None,
) -> tuple[User, str]:
    """Create a user with one key. Returns ``(user, plaintext_token)``."""
    plaintext, token_hash = _new_token()
    user = accounts.create_user(
        label=label, role=role, kind=kind, display_name=display_name, token_hash=token_hash
    )
    logger.info("account created user_id=%d role=%s kind=%s", user.id, role, kind)
    return user, plaintext


def bootstrap_first_admin(accounts: AccountStore, label: str) -> tuple[User, str]:
    """Create the first admin. Raises ``AlreadyBootstrapped`` if an enabled admin exists."""
    plaintext, token_hash = _new_token()
    user = accounts.create_first_admin(label=label, token_hash=token_hash)
    logger.info("first admin bootstrapped user_id=%d", user.id)
    return user, plaintext


def rotate_user_key(
    accounts: AccountStore, user_id: int, *, deliver: Callable[[str], None] | None = None
) -> tuple[User, str]:
    """Revoke the user's active keys, issue one new key and re-enable the user.

    *deliver* (if given) receives the new key inside the rotation's transaction; if it
    raises, nothing is rotated and the old keys keep working.
    """
    plaintext, token_hash = _new_token()
    before = None if deliver is None else (lambda: deliver(plaintext))
    user = accounts.rotate_user_key(user_id, token_hash, before_commit=before)
    logger.info("key rotated user_id=%d key_id=%s", user.id, user.key_id)
    return user, plaintext


# ── invites ───────────────────────────────────────────────────────────────────

INVITE_TTL_S = 600


def join_link(server_url: str, invite_token: str) -> str:
    """The DESIGN §1.7 link the client app consumes (``wispralt://join``)."""
    query = urllib.parse.urlencode({"host": server_url.rstrip("/"), "invite": invite_token})
    return f"wispralt://join?{query}"


def create_invite(
    accounts: AccountStore,
    *,
    created_by: int,
    user_id: int | None = None,
    label: str | None = None,
    role: str = "employee",
    now: float | None = None,
) -> tuple[int, str, float]:
    """A single-use invite (TTL 10 min) for a new person (``label``) or a new device of
    ``user_id``. Returns ``(invite_id, plaintext_token, expires_at)``; only the hash is stored.
    """
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    expires_at = now + INVITE_TTL_S
    invite_id = accounts.create_invite(
        token_hash=hash_token(token),
        created_by=created_by,
        user_id=user_id,
        label=label,
        role=role,
        now=now,
        expires_at=expires_at,
    )
    logger.info("invite created invite_id=%d by user_id=%d target=%s", invite_id, created_by, user_id)
    return invite_id, token, expires_at


def redeem_invite(
    accounts: AccountStore, invite_token: str, device_label: str | None, now: float | None = None
) -> tuple[User, str]:
    """Redeem atomically and issue a new key. ``InviteInvalid`` for every failure."""
    plaintext, key_hash = _new_token()
    user = accounts.redeem_invite(
        invite_hash=hash_token(invite_token),
        key_hash=key_hash,
        device_label=device_label,
        now=time.time() if now is None else now,
    )
    logger.info("invite redeemed user_id=%d key_id=%s", user.id, user.key_id)
    return user, plaintext
