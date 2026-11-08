"""Client <-> server URL contract for file jobs and Local dictation.

Reads the Swift client sources that call the server
(``client/WisprAlt/Server/FileJobAPI.swift``, ``ChunkedUploader.swift`` and
``client/WisprAltCore/LocalRequests.swift``), extracts every ``"/transcribe/..."`` string literal, replaces each Swift
``\\(...)`` interpolation with a concrete sample value, and asserts that the
resulting path is served by a route in the real ``create_app()`` route table
with the method the client sends.

The expected method comes from a small table keyed by the enclosing Swift
function name, not from parsing the Swift (``httpMethod`` / ``method:``), so a
client that moves a call to the wrong verb still fails here. A minimum literal
count stops a regex miss from passing vacuously. Every Local dictation
request (chunk, finalize, whole-file) must send ``X-Client-Dedup-Id`` (Part 4
dedup contract).
"""
from __future__ import annotations

import os

os.environ.setdefault("SERVER_URL", "http://test.local")

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from wispralt.main import create_app

_CLIENT = Path(__file__).resolve().parents[2] / "client"
_LOCAL_REQUESTS = _CLIENT / "WisprAltCore" / "LocalRequests.swift"
_SWIFT_FILES = (
    _CLIENT / "WisprAlt" / "Server" / "FileJobAPI.swift",
    _CLIENT / "WisprAlt" / "Server" / "ChunkedUploader.swift",
    _LOCAL_REQUESTS,
)
_DICTATION_FUNCS = ("chunk", "finalize", "wholeFile")

# Swift function name -> HTTP method the client sends from it.
_EXPECTED_METHOD = {
    "submitFile": "POST",      # POST /transcribe/file
    "upload": "POST",          # chunked init + finalize
    "uploadOneChunk": "POST",  # chunked chunk body
    "poll": "GET",
    "download": "GET",
    "delete": "DELETE",
    "cancel": "DELETE",
    "chunk": "POST",           # LocalRequests: stream chunk
    "finalize": "POST",        # LocalRequests: stream finalize
    "wholeFile": "POST",       # LocalRequests: /transcribe/dictate (+ Retry)
}

# Interpolated Swift expression -> concrete sample value.
_SAMPLE_UUID = "0f8c2a5e-3b1d-4c7e-9a2f-6d4b8e1c0a77"
_SAMPLES = {
    "id.raw": _SAMPLE_UUID,
    "uploadID": _SAMPLE_UUID,
    "chunkIndex": "0",
    "format": "txt",
    "sessionId": _SAMPLE_UUID,
    "index": "0",
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
    for path in _SWIFT_FILES:
        name = path.name
        source = path.read_text(encoding="utf-8")
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
    # submit, chunked init/chunk/finalize, poll, download, delete (+ cancel),
    # and the three Local dictation requests.
    assert len(calls) >= 10, calls
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


def _func_bodies(source: str) -> dict[str, str]:
    """Swift func name -> its text up to the next ``func`` (good enough for one file)."""
    parts = _FUNC_RE.split(source)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_local_dictation_request_sends_the_dedup_header() -> None:
    bodies = _func_bodies(_LOCAL_REQUESTS.read_text(encoding="utf-8"))
    # The shared builder sets the header from its dedup argument ...
    assert re.search(
        r'setValue\(dedupId\.lowercased\(\), forHTTPHeaderField: "X-Client-Dedup-Id"\)',
        bodies["post"],
    ), "LocalRequests.post no longer sets X-Client-Dedup-Id"
    # ... and every dictation request goes through it with its dedupId.
    for func in _DICTATION_FUNCS:
        assert re.search(r"\bpost\([^\n]*\bdedupId\b", bodies[func]), (
            f"LocalRequests.{func} does not pass dedupId to post()"
        )


def test_invite_redeem_server_contract(api_routes: list[APIRoute]) -> None:
    """Part 6 LD 6: the server half of ``wispralt://join?host=&invite=`` (the client handler
    is Part 5). ``POST /invites/redeem`` is public: no auth dependency on the route."""
    from wispralt.accounts.service import join_link

    [route] = [r for r in api_routes if r.path == "/invites/redeem"]
    assert route.methods == {"POST"}
    assert route.dependant.dependencies == []
    assert join_link("https://mini.example.com/", "tok-123") == (
        "wispralt://join?host=https%3A%2F%2Fmini.example.com&invite=tok-123"
    )
