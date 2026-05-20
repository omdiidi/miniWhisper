"""OpenAI-compatible /v1/models endpoint.

Returns the static list of model IDs that clients probe before transcribing.
Open WebUI and several enterprise clients gate on this endpoint — listing the
expected model IDs unblocks their "test connection" flows.

`gpt-4o-transcribe-diarize` is intentionally EXCLUDED — we 404 on it at the
/v1/audio/transcriptions route. Listing-but-rejecting confuses probers.

Auth required (Bearer only via require_api_key_v1). No rate limit — cheap read.
Cache-Control: no-cache so future model-list changes propagate immediately.

Spec reference:
https://platform.openai.com/docs/api-reference/models/list
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from wispralt_server.auth import require_api_key_v1
from wispralt_server.constants import (
    OPENAI_COMPAT_VERSION,
    OPENAI_KNOWN_MODELS,
    OPENAI_KNOWN_MODELS_CREATED,
    OPENAI_KNOWN_MODELS_OWNED_BY,
)
from wispralt_server.users.store import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])

_CACHE_HEADERS = {
    "Cache-Control": "no-cache, must-revalidate",
    "openai-version": OPENAI_COMPAT_VERSION,
}


def _model_object(model_id: str) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "created": OPENAI_KNOWN_MODELS_CREATED,
        "owned_by": OPENAI_KNOWN_MODELS_OWNED_BY,
    }


def _openai_404(request: Request, message: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict = {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": "model_not_found",
        }
    }
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(status_code=404, content=body, headers=_CACHE_HEADERS)


@router.get("/models")
async def list_models(
    request: Request,
    user: User = Depends(require_api_key_v1),
) -> JSONResponse:
    """List models advertised by this WisprAlt instance.

    Returns the static OpenAI-shaped list. 5 models — gpt-4o-transcribe-diarize
    is excluded because we 404 on it at /v1/audio/transcriptions.
    """
    body = {
        "object": "list",
        "data": [_model_object(m) for m in OPENAI_KNOWN_MODELS],
    }
    return JSONResponse(status_code=200, content=body, headers=_CACHE_HEADERS)


@router.get("/models/{model_id}")
async def get_model(
    model_id: str,
    request: Request,
    user: User = Depends(require_api_key_v1),
) -> JSONResponse:
    """Get a single model object by ID. 404s for any model not in the static list."""
    if model_id not in OPENAI_KNOWN_MODELS:
        return _openai_404(
            request,
            f"Model '{model_id}' does not exist on this endpoint. "
            f"Allowed: {list(OPENAI_KNOWN_MODELS)}.",
        )
    return JSONResponse(
        status_code=200,
        content=_model_object(model_id),
        headers=_CACHE_HEADERS,
    )
