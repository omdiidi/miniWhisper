"""Shared test harness: env before any wispralt import, a per-test data_dir and Database."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

# Must run before the first ``wispralt`` import: config.py builds ``settings`` at import.
# DATA_DIR is forced (never a real host's data); the rest only default, as the test files do.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wispralt-test-data-")
os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

import pytest  # noqa: E402

from wispralt.config import settings  # noqa: E402
from wispralt.database import Database  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own data_dir on the module-level ``settings``."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield tmp_path


@pytest.fixture
def db(_data_dir: Path) -> Database:
    database = Database(settings.db_path)
    database.migrate()
    return database
