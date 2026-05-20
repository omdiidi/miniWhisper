"""
test_integration_kind_guards.py — :func:`forbid_integration_kind` coverage.

The /me/* and /telemetry/* surfaces are human-employee-only — integration
keys (``kind='integration'``) must be 403'd. /v1/* is the OPPOSITE: it's the
surface integration keys are FOR, so we also exercise the negative
invariant (integration kind allowed through to /v1).

We stub Postgres + the JobStore everywhere we can; tests focus on the dep
gate, not the downstream business logic.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Pydantic Settings reads these at import time — set defaults BEFORE any
# wispralt_server import so the tests work on a bare dev box without a .env.
os.environ.setdefault("HF_TOKEN", "stub")
os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from wispralt_server.auth import (
    require_api_key,
    require_api_key_v1,
)
from wispralt_server.middleware import openai_errors
from wispralt_server.middleware.cors import install_cors
from wispralt_server.ratelimit_per_token import init_rate_limit_state
from wispralt_server.routes import me as me_routes
from wispralt_server.routes import telemetry as telemetry_routes
from wispralt_server.routes.v1_transcriptions import router as v1_router
from wispralt_server.users.store import User

if TYPE_CHECKING:
    import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Same stand-in as the other v1 tests — sets request_id on request.state."""

    async def dispatch(self, request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or "test-rid"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


class _AlignedTok:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _StubParakeet:
    """Async-mock ParakeetService for the /v1 reach-through test."""

    def __init__(self) -> None:
        self.transcribe_with_alignment = AsyncMock(
            return_value=(
                "hi.",
                1.0,
                [_AlignedTok("hi.", 0.0, 0.5)],
            )
        )


def _override_user(app: FastAPI, *, kind: str, role: str = "employee") -> User:
    """Install dependency overrides for both require_api_key AND
    require_api_key_v1 so the same User shows up everywhere downstream of
    ``forbid_integration_kind`` (which depends on require_api_key)."""
    user = User(id=42, label="test-user", role=role, kind=kind)

    async def _fake(_request: Any = None) -> User:
        return user

    app.dependency_overrides[require_api_key] = _fake
    app.dependency_overrides[require_api_key_v1] = _fake
    return user


# ── /me/history ──────────────────────────────────────────────────────────────


class TestMeHistoryGuard:
    def test_me_history_blocks_integration_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)

        # Set a sentinel pool / job store so the routes don't crash before
        # the guard runs. The guard executes during dep resolution, BEFORE
        # the route body — so most state isn't strictly needed, but
        # AsyncMock for the JobStore methods keeps us safe if it does run.
        app.state.db_pool = object()
        app.state.job_store = MagicMock()

        _override_user(app, kind="integration")
        with TestClient(app) as client:
            resp = client.get("/me/history?range=30d")
        assert resp.status_code == 403, resp.text

    def test_me_history_allows_employee_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        app.state.db_pool = object()
        # JobStore.transcripts_in_range_filtered is invoked via asyncio.to_thread;
        # return a benign empty page so render proceeds.
        store = MagicMock()
        store.transcripts_in_range_filtered = MagicMock(return_value=([], None, None))
        app.state.job_store = store

        _override_user(app, kind="employee")
        with TestClient(app) as client:
            resp = client.get("/me/history?range=30d")
        # Anything not-403 indicates the guard let the request through.
        # 200 is the happy path (empty rows render the base template).
        assert resp.status_code != 403, resp.text
        assert resp.status_code == 200, resp.text


# ── /telemetry/cloud-dictation ───────────────────────────────────────────────


class TestTelemetryGuard:
    def test_telemetry_dictation_blocks_integration_kind(self) -> None:
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(telemetry_routes.router)
        app.state.db_pool = object()
        app.state.job_store = MagicMock()
        _override_user(app, kind="integration")

        with TestClient(app) as client:
            resp = client.post(
                "/telemetry/cloud-dictation",
                json={
                    "dictations": [
                        {
                            "client_dedup_id": "11111111-1111-1111-1111-111111111111",
                            "text": "hello",
                            "dictated_at": 1_700_000_000.0,
                            "word_count": 1,
                            "client_app_version": "0.4.6",
                        },
                    ],
                },
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 403, resp.text


# ── /me/login: must remain unauthenticated ────────────────────────────────────


class TestMeLoginUnauthenticated:
    def test_me_login_get_still_unauthenticated(self) -> None:
        # /me/login MUST NOT require auth — otherwise nobody can log in.
        # No deps overridden; if the route depended on require_api_key
        # transitively, no-auth would return 401.
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        app.state.db_pool = object()
        app.state.job_store = MagicMock()

        with TestClient(app) as client:
            resp = client.get("/me/login")
        assert resp.status_code == 200, resp.text

    def test_me_login_post_still_unauthenticated(self) -> None:
        # POST /me/login with a bogus form should hit the route body's CSRF
        # check (not an auth dep). Expect NOT 401 — actual outcome will be a
        # rendered login form with an error (403 for CSRF failure, or 401 for
        # invalid token format). The point is the route body executes.
        app = FastAPI()
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(me_routes.router)
        app.state.db_pool = object()
        app.state.job_store = MagicMock()

        with TestClient(app) as client:
            resp = client.post(
                "/me/login",
                data={"token": "abc", "csrf_token": "x"},
            )
        # 401 (token format check) and 403 (CSRF failure) both indicate the
        # route body ran — neither implies auth-dep rejection. Reject only an
        # unexpected 5xx or some other status.
        assert resp.status_code in (401, 403), resp.text


# ── /v1 reach-through: integration kind MUST be allowed ───────────────────────


class TestV1AllowsIntegrationKind:
    def test_v1_audio_transcriptions_allows_integration_kind(self) -> None:
        app = FastAPI()
        install_cors(app)
        app.add_middleware(_RequestIdMiddleware)
        app.include_router(v1_router)
        openai_errors.install(app)

        app.state.parakeet_service = _StubParakeet()
        app.state.parakeet_last_inference_at = None
        init_rate_limit_state(app)

        # The WHOLE POINT of integration keys.
        _override_user(app, kind="integration")

        # tiny WAV body — just enough to clear the multipart parser.
        from pathlib import Path

        wav = (Path(__file__).parent / "fixtures" / "tiny.wav").read_bytes()
        with TestClient(app) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("tiny.wav", wav, "audio/wav")},
                data={"model": "whisper-1"},
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == 200, resp.text
