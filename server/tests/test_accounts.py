"""AccountStore + accounts.service: key lookup, rotate, revoke, disable, bootstrap, no token logs."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from wispralt._errors import AlreadyBootstrapped
from wispralt.accounts import service
from wispralt.accounts.service import hash_token
from wispralt.usage.events import UsageEvent
from wispralt.usage.store import UsageStore

if TYPE_CHECKING:
    from conftest import MakeUser

    from wispralt.accounts.store import AccountStore
    from wispralt.database import Database


def _active_keys(db: Database, user_id: int) -> int:
    with db.read() as con:
        return con.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND revoked_at IS NULL", (user_id,)
        ).fetchone()[0]


def test_lookup_valid_revoked_disabled_and_other_user(
    accounts: AccountStore, make_user: MakeUser
) -> None:
    alice, alice_token = make_user(label="alice")
    bob, bob_token = make_user(role="admin", label="bob")

    found = accounts.lookup_key(hash_token(alice_token))
    assert found == alice
    assert found is not None and found.key_id is not None
    assert accounts.lookup_key(hash_token(bob_token)) == bob  # another user's key → that user
    assert accounts.lookup_key(hash_token("not-a-token")) is None

    assert alice.key_id is not None and accounts.revoke_key(alice.key_id)
    assert accounts.lookup_key(hash_token(alice_token)) is None  # revoked key
    assert not accounts.revoke_key(alice.key_id)  # already revoked

    assert accounts.disable_user(bob.id)
    assert accounts.lookup_key(hash_token(bob_token)) is None  # disabled user
    assert not accounts.disable_user(bob.id)


def test_rotate_leaves_exactly_one_active_key(
    db: Database, accounts: AccountStore, make_user: MakeUser
) -> None:
    user, old = make_user()
    rotated, new = service.rotate_user_key(accounts, user.id)
    assert _active_keys(db, user.id) == 1
    assert accounts.lookup_key(hash_token(old)) is None
    assert accounts.lookup_key(hash_token(new)) == rotated
    assert rotated.key_id != user.key_id


def test_rotate_failure_after_revoke_keeps_old_key(
    db: Database, accounts: AccountStore, make_user: MakeUser
) -> None:
    user, old = make_user()
    with db.write() as con:
        con.execute(
            "CREATE TRIGGER fail_key_insert BEFORE INSERT ON api_keys"
            " BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.rotate_user_key(accounts, user.id)
    assert accounts.lookup_key(hash_token(old)) == user
    assert _active_keys(db, user.id) == 1


def test_disable_then_rotate_reenables(accounts: AccountStore, make_user: MakeUser) -> None:
    user, old = make_user()
    accounts.disable_user(user.id)
    rotated, new = service.rotate_user_key(accounts, user.id)
    found = accounts.lookup_key(hash_token(new))
    assert found is not None and found.id == user.id
    assert accounts.lookup_key(hash_token(old)) is None
    assert [r.disabled_at for r in accounts.list_people() if r.id == user.id] == [None]


def test_rotate_unknown_user_is_integrity_error(accounts: AccountStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        service.rotate_user_key(accounts, 999)


def test_create_first_admin_once(accounts: AccountStore) -> None:
    admin, token = service.bootstrap_first_admin(accounts, "owner")
    assert admin.role == "admin"
    assert accounts.lookup_key(hash_token(token)) == admin
    assert accounts.has_enabled_admin()
    with pytest.raises(AlreadyBootstrapped):
        service.bootstrap_first_admin(accounts, "second")
    assert [a.id for a in accounts.list_enabled_admins()] == [admin.id]


def test_first_admin_allowed_when_only_admin_is_disabled(
    accounts: AccountStore, make_user: MakeUser
) -> None:
    old_admin, _ = make_user(role="admin")
    accounts.disable_user(old_admin.id)
    assert not accounts.has_enabled_admin()
    admin, _ = service.bootstrap_first_admin(accounts, "owner")
    assert accounts.list_enabled_admins()[0].id == admin.id


def test_no_token_in_logs(
    caplog: pytest.LogCaptureFixture, accounts: AccountStore, make_user: MakeUser
) -> None:
    caplog.set_level(logging.DEBUG)
    user, t1 = make_user()
    _, t2 = service.rotate_user_key(accounts, user.id)
    _, t3 = service.bootstrap_first_admin(accounts, "owner")
    assert caplog.records  # the service does log the operations
    assert not re.search(r"[0-9a-f]{64}", caplog.text)
    for token in (t1, t2, t3):
        assert re.fullmatch(r"[0-9a-f]{64}", token)


def test_people_integrations_counts_and_last_seen(
    db: Database, accounts: AccountStore, make_user: MakeUser
) -> None:
    emp, _ = make_user(label="emp")
    gone, _ = make_user(label="gone")
    integ, _ = make_user(kind="integration", label="zapier")
    accounts.disable_user(gone.id)
    UsageStore(db).record(
        UsageEvent(user_id=emp.id, ts=1_700_000_000.0, kind="dictate", status=200)
    )

    people = {r.label: r for r in accounts.list_people()}
    assert set(people) == {"emp", "gone"}
    assert people["gone"].disabled_at is not None
    assert people["emp"].last_seen_at is not None
    assert people["emp"].last_seen_at.timestamp() == 1_700_000_000.0
    assert people["emp"].created_at.strftime("%Y")  # templates call .strftime
    assert [r.label for r in accounts.list_people(active=True)] == ["emp"]
    assert [r.label for r in accounts.list_integrations()] == ["zapier"]
    assert accounts.count_kind("employee") == 1
    assert accounts.count_kind("integration") == 1
    accounts.disable_user(integ.id)
    assert accounts.list_integrations() == []


def test_profile_and_display_name(accounts: AccountStore, make_user: MakeUser) -> None:
    user, _ = make_user(label="alice")
    accounts.update_display_name(user.id, "Alice A.")
    profile = accounts.profile(user.id)
    assert profile is not None
    assert (profile.label, profile.display_name, profile.last_seen_at) == (
        "alice",
        "Alice A.",
        None,
    )
    assert abs(profile.created_at.timestamp() - time.time()) < 60
    accounts.disable_user(user.id)
    accounts.update_display_name(user.id, "ignored")
    assert accounts.profile(user.id).display_name == "Alice A."  # type: ignore[union-attr]
    assert accounts.profile(999) is None
    assert accounts.get_user(user.id) is not None  # disabled users are still readable
    assert accounts.get_user(999) is None
