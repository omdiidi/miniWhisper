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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def rotate_user_key(accounts: AccountStore, user_id: int) -> tuple[User, str]:
    """Revoke the user's active keys, issue one new key and re-enable the user."""
    plaintext, token_hash = _new_token()
    user = accounts.rotate_user_key(user_id, token_hash)
    logger.info("key rotated user_id=%d key_id=%s", user.id, user.key_id)
    return user, plaintext
