"""UsageStore: record, overview tiles / top users / daily, user totals, p50, drilldown + CSV."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from wispralt.usage.events import UsageEvent
from wispralt.usage.store import UsageFilters

if TYPE_CHECKING:
    from conftest import MakeUser

    from wispralt.accounts.store import AccountStore, User
    from wispralt.usage.store import UsageStore

DAY = 86_400.0
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC).timestamp()


def _ev(
    user: User, ago: float, *, status: int = 200, kind: str = "dictate", ms: float | None = None
) -> UsageEvent:
    return UsageEvent(
        user_id=user.id,
        ts=NOW - ago,
        kind=kind,
        status=status,
        duration_ms=ms,
        chars=5,
        bytes_in=100,
        bytes_out=10,
        request_id=f"{user.label}-{int(ago)}",
    )


@pytest.fixture
def seeded(usage: UsageStore, accounts: AccountStore, make_user: MakeUser) -> dict[str, User]:
    """alice: 3 in 24h (one 500), 1 at 3d, 1 at 20d; bob: 1 at 2h, 1 at 10d; carol disabled, none."""
    alice, _ = make_user(label="alice")
    bob, _ = make_user(role="admin", label="bob")
    carol, _ = make_user(label="carol")
    accounts.disable_user(carol.id)
    for ev in (
        _ev(alice, 60, ms=100.0),
        _ev(alice, 3600, ms=300.0),
        _ev(alice, 7200, status=500, ms=200.0),
        _ev(alice, 3 * DAY, ms=999.0),
        _ev(alice, 20 * DAY, kind="file"),
        _ev(bob, 2 * 3600, kind="v1", ms=50.0),
        _ev(bob, 10 * DAY),
        _ev(bob, 40 * DAY),  # outside every window
    ):
        usage.record(ev)
    return {"alice": alice, "bob": bob, "carol": carol}


def test_record_unknown_user_is_integrity_error(usage: UsageStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        usage.record(UsageEvent(user_id=999, ts=NOW, kind="dictate", status=200))


def test_overview_matches_fixture(usage: UsageStore, seeded: dict[str, User]) -> None:
    out = usage.overview(NOW)
    assert out["totals"] == {
        "dictations_24h": 4,
        "dictations_7d": 5,
        "dictations_30d": 7,
        "errors_24h": 1,
        "p50_24h": 150.0,  # median of 100, 300, 200, 50
        "total_users": 3,
        "active_users": 2,
    }
    alice, bob, carol = seeded["alice"], seeded["bob"], seeded["carol"]
    assert out["top_users"] == [
        {"id": alice.id, "label": "alice", "role": "employee", "n": 4},
        {"id": bob.id, "label": "bob", "role": "admin", "n": 1},
        {"id": carol.id, "label": "carol", "role": "employee", "n": 0},
    ]
    assert out["daily"] == [
        {"day": "2026-10-28", "n": 1},  # bob, 10 d ago
        {"day": "2026-11-04", "n": 1},  # alice, 3 d ago
        {"day": "2026-11-07", "n": 4},
    ]


def test_user_totals_and_recent_events(usage: UsageStore, seeded: dict[str, User]) -> None:
    alice = seeded["alice"]
    assert usage.user_totals(alice.id, NOW) == {
        "dictations_24h": 3,
        "dictations_7d": 4,
        "dictations_30d": 5,
        "errors_24h": 1,
        "p50_24h": 200.0,  # odd count: 100, 200, 300
    }
    assert usage.user_totals(seeded["carol"].id, NOW) == {
        "dictations_24h": 0,
        "dictations_7d": 0,
        "dictations_30d": 0,
        "errors_24h": 0,
        "p50_24h": None,
    }
    recent = usage.recent_events(alice.id, limit=2)
    assert [e["request_id"] for e in recent] == ["alice-60", "alice-3600"]
    assert recent[0]["ts"] == datetime.fromtimestamp(NOW - 60, tz=UTC)
    assert set(recent[0]) == {
        "ts",
        "kind",
        "status",
        "duration_ms",
        "bytes_in",
        "error_class",
        "request_id",
    }


def test_p50_even_and_odd(usage: UsageStore, make_user: MakeUser) -> None:
    user, _ = make_user()
    for ms in (10.0, 40.0, 20.0):
        usage.record(_ev(user, 60 + ms, ms=ms))
    assert usage.user_totals(user.id, NOW)["p50_24h"] == 20.0
    usage.record(_ev(user, 500, ms=100.0))
    assert usage.user_totals(user.id, NOW)["p50_24h"] == 30.0


def test_drilldown_filters_and_paging(usage: UsageStore, seeded: dict[str, User]) -> None:
    alice = seeded["alice"]
    assert len(usage.events(UsageFilters(), limit=100)) == 8
    page = usage.events(UsageFilters(), limit=2, offset=1)
    assert [e["request_id"] for e in page] == ["alice-3600", "bob-7200"]  # ts tie → newest id first
    only_alice = usage.events(UsageFilters(user_id=alice.id), limit=100)
    assert {e["user_label"] for e in only_alice} == {"alice"} and len(only_alice) == 5
    assert [e["kind"] for e in usage.events(UsageFilters(kind="v1"), limit=100)] == ["v1"]
    assert [e["status"] for e in usage.events(UsageFilters(status=500), limit=100)] == [500]
    since = datetime.fromtimestamp(NOW - 4 * DAY, tz=UTC).replace(tzinfo=None)  # naive = UTC
    until = datetime.fromtimestamp(NOW - 3000, tz=UTC)
    window = usage.events(UsageFilters(since=since, until=until), limit=100)
    assert [e["request_id"] for e in window] == [
        "alice-3600",
        "bob-7200",
        "alice-7200",
        "alice-259200",
    ]


def test_iter_events_csv_rows(usage: UsageStore, seeded: dict[str, User]) -> None:
    rows = list(usage.iter_events(UsageFilters(user_id=seeded["bob"].id)))
    assert [r["kind"] for r in rows] == ["v1", "dictate", "dictate"]
    assert set(rows[0]) == {
        "id",
        "user_id",
        "user_label",
        "ts",
        "kind",
        "status",
        "duration_ms",
        "chars",
        "bytes_in",
        "bytes_out",
        "error_class",
        "request_id",
    }
    assert rows[0]["ts"].isoformat().endswith("+00:00")
    assert len(list(usage.iter_events(UsageFilters(), limit=3))) == 3
