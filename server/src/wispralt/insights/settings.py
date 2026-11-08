"""
insights/settings.py — runtime AI-insights settings, read from the ``settings`` table.

AI interpretation is opt-in: ``insights_enabled`` is ``'0'`` until an admin enables it
after acknowledging the disclosure (no transcript text leaves the host before that).
Pages read it through :func:`ai_enabled`, which treats a missing store or a DB error as
"off" (fail closed).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any


async def ai_enabled(app: Any) -> bool:
    store = getattr(app.state, "settings_store", None)
    if store is None:
        return False
    try:
        return await asyncio.to_thread(store.get, "insights_enabled", "0") == "1"
    except sqlite3.Error:
        return False
