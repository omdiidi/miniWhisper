"""
wispralt.importer — Part 7 history import (Supabase snapshot + transcript files).

The callable surface the Part 5 ``wispralt import …`` CLI wraps; until then
``python -m wispralt.importer`` (``__main__.py``) is the interim entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wispralt.importer.files import import_files as run_file_import
from wispralt.importer.supabase_export import PsqlSource, RowSource, export_snapshot
from wispralt.importer.supabase_import import import_snapshot as run_snapshot_import

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["run_export", "run_file_import", "run_snapshot_import"]


def run_export(backups_root: Path | None = None, source: RowSource | None = None) -> Path:
    """Read-only Supabase export into a new snapshot dir (default ``~/WisprAlt-Backups``)."""
    return export_snapshot(source or PsqlSource(), backups_root)
