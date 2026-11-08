"""
accounts/store.py — ``AccountStore``: users (identity) and api_keys (credentials).

One transaction per method (``Database.read`` / ``Database.write``). A key
authenticates only while it is not revoked **and** its user is not disabled.
``last_seen_at`` is derived from ``MAX(usage_events.ts)`` at read time.
Timestamps are REAL unix seconds in the DB and UTC ``datetime`` values here,
so templates keep ``.strftime``. Plaintext tokens never reach this module:
callers pass ``token_hash`` (see ``accounts.service``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from wispralt._errors import AlreadyBootstrapped, InviteInvalid

if TYPE_CHECKING:
    import sqlite3

    from wispralt.database import Database


@dataclass(frozen=True, slots=True)
class User:
    """Auth-time identity. ``key_id`` is the key that authenticated, if any."""

    id: int
    key_id: int | None
    label: str
    role: str  # 'admin' | 'employee'
    kind: str  # 'employee' | 'integration'


@dataclass(frozen=True, slots=True)
class UserRow:
    """Admin-UI row."""

    id: int
    label: str
    role: str
    created_at: datetime
    disabled_at: datetime | None
    last_seen_at: datetime | None
    display_name: str | None
    kind: str


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Read-only snapshot for /me and the admin user-detail page."""

    id: int
    label: str
    display_name: str | None
    role: str
    created_at: datetime
    last_seen_at: datetime | None
    disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KeyRow:
    """One api_keys row (a device; never its hash) with its owner and derived last use."""

    id: int
    user_id: int
    user_label: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class InviteRow:
    """Admin-UI invite row. ``status``: pending | expired | redeemed."""

    id: int
    label: str | None
    role: str
    user_id: int | None
    user_label: str | None
    created_by_label: str
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    status: str


def _dt(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC)


_ROW_SQL = """
SELECT u.id, u.label, u.role, u.created_at, u.disabled_at, u.display_name, u.kind,
       (SELECT MAX(e.ts) FROM usage_events e WHERE e.user_id = u.id) AS last_seen_at
  FROM users u
"""


def _user_row(r: sqlite3.Row) -> UserRow:
    return UserRow(
        id=r["id"],
        label=r["label"],
        role=r["role"],
        created_at=datetime.fromtimestamp(r["created_at"], tz=UTC),
        disabled_at=_dt(r["disabled_at"]),
        last_seen_at=_dt(r["last_seen_at"]),
        display_name=r["display_name"],
        kind=r["kind"],
    )


def _user(r: sqlite3.Row, key_id: int | None = None) -> User:
    return User(id=r["id"], key_id=key_id, label=r["label"], role=r["role"], kind=r["kind"])


class AccountStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── reads ────────────────────────────────────────────────────────────────

    def lookup_key(self, token_hash: str) -> User | None:
        """The enabled user owning this active key, else None."""
        with self.db.read() as con:
            r = con.execute(
                "SELECT u.id, u.label, u.role, u.kind, k.id AS key_id"
                "  FROM api_keys k JOIN users u ON u.id = k.user_id"
                " WHERE k.token_hash = ? AND k.revoked_at IS NULL AND u.disabled_at IS NULL",
                (token_hash,),
            ).fetchone()
        return None if r is None else _user(r, r["key_id"])

    def get_user(self, user_id: int) -> User | None:
        """Any user (disabled or not) by id."""
        with self.db.read() as con:
            r = con.execute(
                "SELECT id, label, role, kind FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return None if r is None else _user(r)

    def list_people(self, *, active: bool = False) -> list[UserRow]:
        """Employee-kind users, newest first; ``active`` hides disabled ones."""
        where = " WHERE u.kind = 'employee'" + (" AND u.disabled_at IS NULL" if active else "")
        with self.db.read() as con:
            rows = con.execute(_ROW_SQL + where + " ORDER BY u.created_at DESC, u.id DESC")
            return [_user_row(r) for r in rows]

    def list_integrations(self) -> list[UserRow]:
        """Enabled integration-kind users, newest first."""
        with self.db.read() as con:
            rows = con.execute(
                _ROW_SQL + " WHERE u.kind = 'integration' AND u.disabled_at IS NULL"
                " ORDER BY u.created_at DESC, u.id DESC"
            )
            return [_user_row(r) for r in rows]

    def count_kind(self, kind: str) -> int:
        """Enabled users of *kind*."""
        with self.db.read() as con:
            return con.execute(
                "SELECT COUNT(*) FROM users WHERE kind = ? AND disabled_at IS NULL", (kind,)
            ).fetchone()[0]

    def has_enabled_admin(self) -> bool:
        return bool(self.list_enabled_admins())

    def list_enabled_admins(self) -> list[User]:
        with self.db.read() as con:
            rows = con.execute(
                "SELECT id, label, role, kind FROM users"
                " WHERE role = 'admin' AND disabled_at IS NULL ORDER BY id"
            )
            return [_user(r) for r in rows]

    def list_keys(self, user_id: int | None = None) -> list[KeyRow]:
        """Keys (devices), active first then newest, with last seen; optionally one user's."""
        where, params = (" WHERE k.user_id = ?", (user_id,)) if user_id is not None else ("", ())
        with self.db.read() as con:
            rows = con.execute(
                "SELECT k.id, k.user_id, u.label AS user_label, k.label, k.created_at, k.revoked_at,"
                " (SELECT MAX(e.ts) FROM usage_events e WHERE e.key_id = k.id) AS last_seen_at"
                " FROM api_keys k JOIN users u ON u.id = k.user_id"
                + where
                + " ORDER BY k.revoked_at IS NOT NULL, k.created_at DESC, k.id DESC",
                params,
            )
            return [
                KeyRow(
                    id=r["id"],
                    user_id=r["user_id"],
                    user_label=r["user_label"],
                    label=r["label"],
                    created_at=datetime.fromtimestamp(r["created_at"], tz=UTC),
                    revoked_at=_dt(r["revoked_at"]),
                    last_seen_at=_dt(r["last_seen_at"]),
                )
                for r in rows
            ]

    def key_owner(self, key_id: int) -> int | None:
        with self.db.read() as con:
            r = con.execute("SELECT user_id FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return None if r is None else int(r[0])

    def list_invites(self, now: float, limit: int = 100) -> list[InviteRow]:
        """Newest first, with target and creator labels and a derived status."""
        with self.db.read() as con:
            rows = con.execute(
                "SELECT i.id, i.label, i.role, i.user_id, i.created_at, i.expires_at,"
                " i.redeemed_at, t.label AS user_label, c.label AS created_by_label"
                " FROM invites i JOIN users c ON c.id = i.created_by"
                " LEFT JOIN users t ON t.id = i.user_id"
                " ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                (limit,),
            )
            return [
                InviteRow(
                    id=r["id"],
                    label=r["label"],
                    role=r["role"],
                    user_id=r["user_id"],
                    user_label=r["user_label"],
                    created_by_label=r["created_by_label"],
                    created_at=datetime.fromtimestamp(r["created_at"], tz=UTC),
                    expires_at=datetime.fromtimestamp(r["expires_at"], tz=UTC),
                    redeemed_at=_dt(r["redeemed_at"]),
                    status="redeemed" if r["redeemed_at"] is not None
                    else "expired" if r["expires_at"] <= now else "pending",
                )
                for r in rows
            ]

    def profile(self, user_id: int) -> UserProfile | None:
        """Any user (disabled or not), with derived ``last_seen_at``."""
        with self.db.read() as con:
            r = con.execute(_ROW_SQL + " WHERE u.id = ?", (user_id,)).fetchone()
        if r is None:
            return None
        return UserProfile(
            id=r["id"],
            label=r["label"],
            display_name=r["display_name"],
            role=r["role"],
            created_at=datetime.fromtimestamp(r["created_at"], tz=UTC),
            last_seen_at=_dt(r["last_seen_at"]),
            disabled_at=_dt(r["disabled_at"]),
        )

    # ── writes ───────────────────────────────────────────────────────────────

    def update_display_name(self, user_id: int, display_name: str | None) -> None:
        """Caller validates the value. Disabled users are skipped."""
        with self.db.write() as con:
            con.execute(
                "UPDATE users SET display_name = ? WHERE id = ? AND disabled_at IS NULL",
                (display_name, user_id),
            )

    @staticmethod
    def _insert_user_with_key(
        con: sqlite3.Connection,
        *,
        label: str,
        role: str,
        kind: str,
        display_name: str | None,
        token_hash: str,
    ) -> User:
        now = time.time()
        user_id = con.execute(
            "INSERT INTO users(label, display_name, role, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?) RETURNING id",
            (label, display_name, role, kind, now),
        ).fetchone()[0]
        key_id = con.execute(
            "INSERT INTO api_keys(user_id, token_hash, created_at) VALUES (?, ?, ?) RETURNING id",
            (user_id, token_hash, now),
        ).fetchone()[0]
        return User(id=user_id, key_id=key_id, label=label, role=role, kind=kind)

    def create_user(
        self,
        *,
        label: str,
        role: str,
        kind: str,
        display_name: str | None,
        token_hash: str,
    ) -> User:
        """Insert a user and its first key in one transaction."""
        with self.db.write() as con:
            return self._insert_user_with_key(
                con,
                label=label,
                role=role,
                kind=kind,
                display_name=display_name,
                token_hash=token_hash,
            )

    def create_first_admin(self, *, label: str, token_hash: str) -> User:
        """Create the first admin; ``AlreadyBootstrapped`` if an enabled admin exists."""
        with self.db.write() as con:
            if con.execute(
                "SELECT 1 FROM users WHERE role = 'admin' AND disabled_at IS NULL LIMIT 1"
            ).fetchone():
                raise AlreadyBootstrapped("an enabled admin already exists")
            return self._insert_user_with_key(
                con,
                label=label,
                role="admin",
                kind="employee",
                display_name=None,
                token_hash=token_hash,
            )

    def rotate_user_key(self, user_id: int, token_hash: str) -> User:
        """Revoke every active key of the user, add one new key and re-enable the user.

        One transaction: on any failure the old keys stay valid.
        ``sqlite3.IntegrityError`` if the user does not exist.
        """
        now = time.time()
        with self.db.write() as con:
            con.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            key_id = con.execute(
                "INSERT INTO api_keys(user_id, token_hash, created_at) VALUES (?, ?, ?) RETURNING id",
                (user_id, token_hash, now),
            ).fetchone()[0]
            con.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (user_id,))
            r = con.execute(
                "SELECT id, label, role, kind FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user(r, key_id)

    def revoke_key(self, key_id: int) -> bool:
        """Revoke one key (single-device removal). False if already revoked or unknown."""
        with self.db.write() as con:
            return (
                con.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (time.time(), key_id),
                ).rowcount
                == 1
            )

    def disable_user(self, user_id: int) -> bool:
        """Disable a user: none of its keys authenticate. False if already disabled or unknown."""
        with self.db.write() as con:
            return (
                con.execute(
                    "UPDATE users SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                    (time.time(), user_id),
                ).rowcount
                == 1
            )

    # ── invites (single use; the token is stored only as its sha256) ─────────

    def create_invite(
        self,
        *,
        token_hash: str,
        created_by: int,
        user_id: int | None,
        label: str | None,
        role: str,
        now: float,
        expires_at: float,
    ) -> int:
        """Insert one invite. For "add device" (``user_id`` set) the target must be an
        enabled employee-kind user, else ``LookupError``."""
        with self.db.write() as con:
            if user_id is not None and not con.execute(
                "SELECT 1 FROM users WHERE id = ? AND kind = 'employee' AND disabled_at IS NULL",
                (user_id,),
            ).fetchone():
                raise LookupError("invite target is not an enabled person")
            return con.execute(
                "INSERT INTO invites(token_hash, user_id, label, role, created_by, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (token_hash, user_id, label, role, created_by, now, expires_at),
            ).fetchone()[0]

    def revoke_invite(self, invite_id: int, now: float) -> bool:
        """Expire an unredeemed, unexpired invite now. False if it was not pending."""
        with self.db.write() as con:
            return (
                con.execute(
                    "UPDATE invites SET expires_at = ?"
                    " WHERE id = ? AND redeemed_at IS NULL AND expires_at > ?",
                    (now, invite_id, now),
                ).rowcount
                == 1
            )

    def redeem_invite(
        self, *, invite_hash: str, key_hash: str, device_label: str | None, now: float
    ) -> User:
        """Redeem in ONE ``BEGIN IMMEDIATE``: validate, create the person if the invite is
        for a new one, add a key, mark the invite used. Any failure → ``InviteInvalid`` and
        nothing is written. Never revokes the user's other keys."""
        with self.db.write() as con:
            inv = con.execute(
                "SELECT id, user_id, label, role, created_by, expires_at, redeemed_at"
                " FROM invites WHERE token_hash = ?",
                (invite_hash,),
            ).fetchone()
            if inv is None or inv["redeemed_at"] is not None or inv["expires_at"] <= now:
                raise InviteInvalid
            if not con.execute(
                "SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND disabled_at IS NULL",
                (inv["created_by"],),
            ).fetchone():
                raise InviteInvalid
            if inv["user_id"] is None:
                user_id = con.execute(
                    "INSERT INTO users(label, role, kind, created_at)"
                    " VALUES (?, ?, 'employee', ?) RETURNING id",
                    (inv["label"], inv["role"], now),
                ).fetchone()[0]
            else:
                user_id = inv["user_id"]
                if not con.execute(
                    "SELECT 1 FROM users WHERE id = ? AND kind = 'employee'"
                    " AND disabled_at IS NULL",
                    (user_id,),
                ).fetchone():
                    raise InviteInvalid
            key_id = con.execute(
                "INSERT INTO api_keys(user_id, token_hash, label, created_at)"
                " VALUES (?, ?, ?, ?) RETURNING id",
                (user_id, key_hash, device_label, now),
            ).fetchone()[0]
            if con.execute(
                "UPDATE invites SET redeemed_at = ?, api_key_id = ?"
                " WHERE id = ? AND redeemed_at IS NULL",
                (now, key_id, inv["id"]),
            ).rowcount != 1:
                raise InviteInvalid
            r = con.execute(
                "SELECT id, label, role, kind FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user(r, key_id)
