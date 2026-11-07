"""
middleware/cors.py — CORS middleware install helper.

Installed as the OUTERMOST middleware in main.py so EVERY response (including
rate-limit 429 envelopes) carries Access-Control-Allow-Origin. Browser clients
then see real HTTP errors instead of opaque "CORS error" failures.

Scope: global. allow_origin="*" with allow_credentials=False — admin endpoints
can't return cookies cross-origin, so leaking ACAO on /admin/* is safe.
Same-origin admin UI on transcribe.integrateapi.ai unaffected.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI


# OpenAI SDK + Stainless-generated SDK headers + standard auth header
_ALLOWED_HEADERS = [
    "Authorization",
    "Content-Type",
    "OpenAI-Organization",
    "OpenAI-Project",
    "OpenAI-Beta",
    "X-Stainless-Lang",
    "X-Stainless-Package-Version",
    "X-Stainless-OS",
    "X-Stainless-Arch",
    "X-Stainless-Runtime",
    "X-Stainless-Runtime-Version",
    "User-Agent",
]

# Response headers exposed to browser clients (for diagnostics)
_EXPOSED_HEADERS = [
    "x-request-id",
    "openai-version",
    "openai-processing-ms",
    "openai-model",
    "retry-after",
]


def install_cors(app: FastAPI) -> None:
    """Install CORSMiddleware globally.

    NOTE: call this AFTER all other `app.add_middleware()` calls in main.py.
    Starlette's middleware stack is LIFO — the LAST middleware added is the
    OUTERMOST (first to see incoming requests, last to see outgoing responses).
    This ordering guarantees CORS headers attach to every response, including
    rate-limit 429 envelopes and exception-handler errors, so browser clients
    surface the real HTTP error instead of an opaque "CORS error".
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,  # required when allow_origins=["*"] per CORS spec
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=_ALLOWED_HEADERS,
        expose_headers=_EXPOSED_HEADERS,
        max_age=86400,
    )
