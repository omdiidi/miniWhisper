"""FastAPI exception handlers scoped to /v1/* — re-shape errors to OpenAI envelope.

Native WisprAlt routes (/transcribe/*, /me, /admin/*) keep the default FastAPI
{"detail": "..."} shape. Only /v1/* paths get the OpenAI-compat envelope.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _is_v1(request: Request) -> bool:
    return request.url.path.startswith("/v1/")


def _openai_envelope(
    message: str,
    type_: str,
    code: str,
    status: int,
    request_id: str | None,
    headers: dict | None = None,
) -> JSONResponse:
    body: dict = {
        "error": {"message": message, "type": type_, "param": None, "code": code}
    }
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(status_code=status, content=body, headers=headers)


def install(app: FastAPI) -> None:
    """Register /v1-scoped exception handlers. Call from create_app() AFTER routes are registered."""

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        # Forward exc.headers (e.g., WWW-Authenticate on 401) on both branches.
        headers = exc.headers if getattr(exc, "headers", None) else None
        if not _is_v1(request):
            # Fall through to default-shape for native routes.
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=headers,
            )
        rid = getattr(request.state, "request_id", None)
        # Map status codes to OpenAI error types.
        if exc.status_code == 401:
            return _openai_envelope(
                str(exc.detail), "invalid_request_error", "invalid_api_key", 401, rid, headers
            )
        if exc.status_code == 403:
            return _openai_envelope(
                str(exc.detail), "invalid_request_error", "forbidden", 403, rid, headers
            )
        if exc.status_code == 429:
            return _openai_envelope(
                str(exc.detail), "rate_limit_error", "rate_limit_exceeded", 429, rid, headers
            )
        if 400 <= exc.status_code < 500:
            return _openai_envelope(
                str(exc.detail),
                "invalid_request_error",
                "bad_request",
                exc.status_code,
                rid,
                headers,
            )
        return _openai_envelope(
            str(exc.detail),
            "server_error",
            "internal_error",
            exc.status_code,
            rid,
            headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not _is_v1(request):
            return JSONResponse(status_code=422, content={"detail": exc.errors()})
        rid = getattr(request.state, "request_id", None)
        # Pull the first error's message (OpenAI envelope is single-message).
        msg = "Invalid request"
        errs = exc.errors()
        if errs:
            loc = ".".join(str(s) for s in errs[0].get("loc", []))
            msg = f"{errs[0].get('msg', 'Invalid request')} ({loc})"
        return _openai_envelope(
            msg, "invalid_request_error", "validation_failed", 422, rid
        )
