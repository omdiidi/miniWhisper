"""Shared Jinja2Templates singleton for the admin/employee web UI.

Jinja2's default autoescape list does NOT include ``.html.j2`` — without
this opt-in, user-supplied fields (labels, errors, etc.) render unescaped,
opening a stored-XSS hole. Resolve the template dir relative to this file
so launchd's CWD doesn't matter.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape

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
