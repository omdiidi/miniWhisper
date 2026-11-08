"""
insights/settings.py — runtime AI-insights settings, stored in the DB ``settings`` table.

AI interpretation is opt-in: ``insights_enabled`` stays ``'0'`` until an admin enables
it at ``/admin/settings`` after acknowledging the disclosure
(``insights_disclosure_ack_at``); no transcript text leaves the host before that. The
other keys: ``insights_model`` (default ``config.insights_model``),
``insights_cap_30d_usd`` (default 8), ``insights_timezone`` (default
``config.insights_timezone``, else the host zone, else UTC). The OpenRouter key is
never stored here: it comes only from the environment (the host service reads
``~/.config/wispralt/.env``).

Readers fail closed: no store, or a DB error, means AI off.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from wispralt.config import settings as config
from wispralt.insights.timewindow import host_timezone

if TYPE_CHECKING:
    from wispralt.settings_store import SettingsStore

ENABLED = "insights_enabled"
ACK_AT = "insights_disclosure_ack_at"
MODEL = "insights_model"
CAP = "insights_cap_30d_usd"
TIMEZONE = "insights_timezone"
DEFAULT_CAP_USD = 8.0


def resolve_timezone(stored: str | None) -> str:
    return stored or config.insights_timezone or host_timezone()


@dataclass(frozen=True, slots=True)
class InsightsSettings:
    enabled: bool
    disclosure_ack_at: float | None
    model: str
    cap_30d_usd: float
    timezone: str


def load(store: SettingsStore | None) -> InsightsSettings:
    values: dict[str, str] = {}
    if store is not None:
        try:
            values = store.get_all()
        except sqlite3.Error:
            values = {}
    try:
        cap = float(values.get(CAP, DEFAULT_CAP_USD))
    except ValueError:
        cap = 0.0  # unreadable cap: spend nothing
    ack = values.get(ACK_AT)
    return InsightsSettings(
        enabled=values.get(ENABLED) == "1" and ack is not None,
        disclosure_ack_at=float(ack) if ack else None,
        model=values.get(MODEL) or config.insights_model,
        cap_30d_usd=cap,
        timezone=resolve_timezone(values.get(TIMEZONE)),
    )


async def load_for(app: Any) -> InsightsSettings:
    return await asyncio.to_thread(load, getattr(app.state, "settings_store", None))


async def ai_enabled(app: Any) -> bool:
    return (await load_for(app)).enabled


async def timezone_for(app: Any) -> str:
    """The insights/history timezone for page math (DB setting > config > host > UTC)."""
    return (await load_for(app)).timezone


def save(
    store: SettingsStore,
    *,
    enabled: bool,
    acknowledge: bool,
    model: str,
    cap_30d_usd: float,
    timezone: str | None,
    now: float | None = None,
) -> None:
    """Persist the settings form. ``ValueError`` when enabling without a disclosure ack."""
    current = load(store)
    ack_at = current.disclosure_ack_at
    if acknowledge and ack_at is None:
        ack_at = time.time() if now is None else now
    if enabled and ack_at is None:
        raise ValueError("Read and accept the disclosure before turning AI on.")
    store.set_many({
        ENABLED: "1" if enabled else "0",
        ACK_AT: None if ack_at is None else repr(ack_at),
        MODEL: model,
        CAP: repr(float(cap_30d_usd)),
        TIMEZONE: timezone or None,
    })
