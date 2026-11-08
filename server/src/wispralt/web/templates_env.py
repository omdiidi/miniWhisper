"""Shared Jinja2Templates singleton for the admin/employee web UI.

Jinja2's default autoescape list does NOT include ``.html.j2`` — without
this opt-in, user-supplied fields (labels, errors, etc.) render unescaped,
opening a stored-XSS hole. Resolve the template dir relative to this file
so launchd's CWD doesn't matter.

``highlight`` (also the ``|highlight`` filter) renders an FTS5 snippet: the raw
text is HTML-escaped FIRST, then the ``\\x02…\\x03`` match sentinels become
``<mark>…</mark>``. Never mark a raw snippet ``|safe``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape
from markupsafe import Markup, escape

_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "admin" / "templates"
)

templates = Jinja2Templates(
    directory=str(_TEMPLATES_DIR),
    autoescape=select_autoescape(
        enabled_extensions=("html.j2", "html", "j2"),
        default_for_string=False,
    ),
)


def highlight(snippet: str | None) -> Markup:
    """Escape *snippet*, then wrap the sentinel-delimited matches in ``<mark>``."""
    escaped = str(escape(snippet or ""))
    return Markup(escaped.replace("\x02", "<mark>").replace("\x03", "</mark>"))


def ts_utc(ts: float | None) -> str:
    """Unix seconds → ``YYYY-MM-DD HH:MM UTC`` (``—`` for None)."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["highlight"] = highlight
templates.env.filters["ts"] = ts_utc
