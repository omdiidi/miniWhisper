"""
test_v1_models.py — Coverage for /v1/models (OpenAI-compat model listing).

The route returns a hard-coded list (no DB). We exercise:

  - the count + exclusion invariant (5 models; gpt-4o-transcribe-diarize
    intentionally NOT advertised because we 404 on it at /v1/audio/transcriptions);
  - 404 on unknown ids (including the explicitly-excluded diarize id);
  - the Cache-Control + openai-version response headers;
  - auth: Bearer required, cookie-only rejected.
"""
from __future__ import annotations

import os
from typing import Any

# Pydantic Settings reads these at import time — set defaults BEFORE any
# wispralt_server import so the tests work on a bare dev box without a .env.
os.environ.setdefault("HF_TOKEN", "stub")
os.environ.setdefault("WISPRALT_API_KEY", "stub_api_key_for_tests_only")
os.environ.setdefault("SERVER_URL", "http://test.local")
os.environ.setdefault("MEETING_OUTPUT_DIR", "/tmp/wispralt-test-meeting")
os.environ.setdefault("JOB_DB_PATH", "/tmp/wispralt-test-jobs.db")
os.environ.setdefault("STAGING_DIR", "/tmp/wispralt-test-staging")

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from wispralt_server.auth import require_api_key_v1
from wispralt_server.middleware import openai_errors
from wispralt_server.routes.v1_models import router as v1_models_router
from wispralt_server.users.store import User


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Stand-in for main.py's _RequestIdMiddleware — see test_v1_transcriptions.py."""

    async def dispatch(self, request, call_next):  # type: ignore[override]
        rid = request.headers.get("x-request-id") or "test-rid"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


def _build_app(*, override_auth: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_RequestIdMiddleware)
    app.include_router(v1_models_router)
    openai_errors.install(app)

    if override_auth:
        async def _fake(_request: Any = None) -> User:
            return User(
                id=99,
                label="key-test",
                role="employee",
                kind="integration",
            )

        app.dependency_overrides[require_api_key_v1] = _fake
    return app


# ── listing ───────────────────────────────────────────────────────────────────


def test_list_returns_5_models() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/v1/models", headers={"Authorization": "Bearer test"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 5


def test_list_no_diarize_model() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/v1/models", headers={"Authorization": "Bearer test"})
    assert resp.status_code == 200, resp.text
    ids = [m["id"] for m in resp.json()["data"]]
    assert "gpt-4o-transcribe-diarize" not in ids


# ── single-model fetch ────────────────────────────────────────────────────────


def test_get_whisper_1() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/v1/models/whisper-1", headers={"Authorization": "Bearer test"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "whisper-1"
    assert body["object"] == "model"


def test_get_unknown_returns_404() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/v1/models/foo", headers={"Authorization": "Bearer test"})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "model_not_found"


def test_get_diarize_returns_404() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/v1/models/gpt-4o-transcribe-diarize",
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404, resp.text


# ── headers ───────────────────────────────────────────────────────────────────


def test_cache_control_no_cache() -> None:
    app = _build_app()
    with TestClient(app) as client:
        list_resp = client.get(
            "/v1/models", headers={"Authorization": "Bearer test"}
        )
        single_resp = client.get(
            "/v1/models/whisper-1", headers={"Authorization": "Bearer test"}
        )
    for resp in (list_resp, single_resp):
        cc = resp.headers.get("cache-control", "").lower()
        assert "no-cache" in cc
        assert "must-revalidate" in cc


def test_response_has_openai_version_header() -> None:
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/v1/models", headers={"Authorization": "Bearer test"})
    assert resp.headers.get("openai-version")


# ── auth ─────────────────────────────────────────────────────────────────────


def test_requires_bearer_auth() -> None:
    # Let the real require_api_key_v1 run — no auth headers means 401.
    app = _build_app(override_auth=False)
    with TestClient(app) as client:
        resp = client.get("/v1/models")
    assert resp.status_code == 401, resp.text


def test_cookie_only_auth_rejected() -> None:
    # /v1/models uses require_api_key_v1 which IGNORES cookies. Setting the
    # admin cookie without a Bearer header MUST 401.
    app = _build_app(override_auth=False)
    with TestClient(app) as client:
        resp = client.get(
            "/v1/models",
            cookies={"wispralt_admin_token": "deadbeef" * 8},
        )
    assert resp.status_code == 401, resp.text
