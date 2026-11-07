"""Client <-> server URL contract for uploaded-file transcription.

Reads the Swift client sources that call the file-job API
(``client/WisprAlt/Server/FileJobAPI.swift`` and ``ChunkedUploader.swift``),
extracts every ``"/transcribe/..."`` string literal, replaces each Swift
``\\(...)`` interpolation with a concrete sample value, and asserts that the
resulting path is served by a route in the real ``create_app()`` route table
with the method the client sends.

The expected method comes from a small table keyed by the enclosing Swift
function name, not from parsing the Swift (``httpMethod`` / ``method:``), so a
client that moves a call to the wrong verb still fails here. A minimum literal
count stops a regex miss from passing vacuously.
"""
from __future__ import annotations

import os

os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from wispralt.main import create_app

_CLIENT_SERVER_DIR = (
    Path(__file__).resolve().parents[2] / "client" / "WisprAlt" / "Server"
)
_SWIFT_FILES = ("FileJobAPI.swift", "ChunkedUploader.swift")

# Swift function name -> HTTP method the client sends from it.
_EXPECTED_METHOD = {
    "submitFile": "POST",      # POST /transcribe/file
    "upload": "POST",          # chunked init + finalize
    "uploadOneChunk": "POST",  # chunked chunk body
    "poll": "GET",
    "download": "GET",
    "delete": "DELETE",
    "cancel": "DELETE",
}

# Interpolated Swift expression -> concrete sample value.
_SAMPLE_UUID = "0f8c2a5e-3b1d-4c7e-9a2f-6d4b8e1c0a77"
_SAMPLES = {
    "id.raw": _SAMPLE_UUID,
    "uploadID": _SAMPLE_UUID,
    "chunkIndex": "0",
    "format": "txt",
}

_LITERAL_RE = re.compile(r'"(/transcribe/[^"]*)"')
_FUNC_RE = re.compile(r"\bfunc\s+(\w+)")
_INTERP_RE = re.compile(r"\\\(([^)]*)\)")


def _concrete(literal: str) -> str:
    def sub(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        if expr not in _SAMPLES:
            raise AssertionError(
                f"no sample value for Swift interpolation \\({expr}) in {literal!r}; "
                "add it to _SAMPLES"
            )
        return _SAMPLES[expr]

    return _INTERP_RE.sub(sub, literal)


def _client_calls() -> list[tuple[str, str, str, str]]:
    """Return (file, swift_func, raw_literal, concrete_path) for every literal."""
    calls: list[tuple[str, str, str, str]] = []
    for name in _SWIFT_FILES:
        source = (_CLIENT_SERVER_DIR / name).read_text(encoding="utf-8")
        current_func: str | None = None
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("//"):
                continue  # doc comments describe paths in backticks; skip anyway
            fm = _FUNC_RE.search(stripped)
            if fm:
                current_func = fm.group(1)
            for lm in _LITERAL_RE.finditer(stripped):
                assert current_func is not None, f"{name}: literal outside a func: {stripped}"
                raw = lm.group(1)
                calls.append((name, current_func, raw, _concrete(raw)))
    return calls


@pytest.fixture(scope="module")
def api_routes() -> list[APIRoute]:
    app = create_app()
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_enough_client_literals_found() -> None:
    calls = _client_calls()
    # submit, chunked init/chunk/finalize, poll, download, delete (+ cancel).
    assert len(calls) >= 7, calls
    funcs = {func for _, func, _, _ in calls}
    missing = set(_EXPECTED_METHOD) - funcs
    assert not missing, f"expected client funcs without a /transcribe literal: {missing}"


def test_every_client_path_is_served(api_routes: list[APIRoute]) -> None:
    failures: list[str] = []
    for file, func, raw, path in _client_calls():
        method = _EXPECTED_METHOD.get(func)
        if method is None:
            failures.append(f"{file}:{func} uses {raw!r} but has no expected method")
            continue
        served = any(
            route.path_regex.fullmatch(path) and method in (route.methods or set())
            for route in api_routes
        )
        if not served:
            failures.append(f"{file}:{func} {method} {path} (from {raw!r}) has no route")
    assert failures == [], "\n".join(failures)
