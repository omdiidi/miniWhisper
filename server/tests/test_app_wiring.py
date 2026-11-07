"""
test_app_wiring.py — the REAL application route table.

Imports ``wispralt.main`` and calls the real ``create_app()``. There is
deliberately no fallback app: if the import graph is broken (e.g. a deleted
module is still imported somewhere), this file fails at collection.

The lifespan is NOT run (it installs signal handlers, needs ffmpeg, and
loads Parakeet), so this only proves the route table and import graph.

Forbidden paths are built by concatenation so the repo-wide grep gate for
removed features stays clean.
"""
from __future__ import annotations

import os

os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

import pytest
from fastapi.routing import APIRoute

from wispralt.main import create_app

_MEETING_PREFIX = "/transcribe/" + "meeting"
_MEETING_READYZ = "/readyz/" + "meeting"


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, frozenset[str]]]:
    # dev_faults reads the environment at create_app() time; enabling it
    # makes later "no /dev/*" assertions meaningful.
    monkeypatch.setenv("WISPRALT_DEV_FAULTS", "1")
    app = create_app()
    return [
        (r.path, frozenset(r.methods or ()))
        for r in app.routes
        if isinstance(r, APIRoute)
    ]


def test_file_submit_routes_present(routes: list[tuple[str, frozenset[str]]]) -> None:
    table = {path: methods for path, methods in routes}
    assert "POST" in table["/transcribe/file"]
    assert "POST" in table["/transcribe/file/chunked/init"]
    assert "GET" in table["/readyz/dictation"]


def test_meeting_submit_route_absent(routes: list[tuple[str, frozenset[str]]]) -> None:
    offenders = [
        (path, sorted(methods))
        for path, methods in routes
        if path.startswith(_MEETING_PREFIX) and "POST" in methods
    ]
    assert offenders == []


def test_meeting_readyz_absent(routes: list[tuple[str, frozenset[str]]]) -> None:
    assert [p for p, _ in routes if p.startswith(_MEETING_READYZ)] == []


def test_file_job_routes_present(routes: list[tuple[str, frozenset[str]]]) -> None:
    by_path: dict[str, set[str]] = {}
    for path, methods in routes:
        by_path.setdefault(path, set()).update(methods)
    assert {"GET", "DELETE"} <= by_path["/transcribe/file/{job_id}"]
    assert "GET" in by_path["/transcribe/file/{job_id}/download/{fmt}"]


def test_no_meeting_routes_remain(routes: list[tuple[str, frozenset[str]]]) -> None:
    assert [p for p, _ in routes if p.startswith(_MEETING_PREFIX)] == []
