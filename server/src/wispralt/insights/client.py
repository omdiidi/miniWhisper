"""OpenRouter caller for weekly insights — Phase 2.

Mirrors smart_format.mercury_client.MercuryClient:
  - asyncio.wait_for wrapping httpx.AsyncClient.post for a hard wall-clock budget
    (httpx.Timeout only bounds phases — not the full request).
  - Explicit Authorization + X-Title headers (X-Title is the OpenRouter
    convention for attributing usage to an app).
  - aclose() coroutine for graceful shutdown.

Differences from Mercury:
  - longer timeout (30 s default, vs Mercury's 600 ms — these are batch, not live).
  - JSON-mode requested via ``response_format={"type": "json_object"}``.
  - Cost computed from response body (``usage.cost``, NOT ``usage.total_cost`` —
    Task 0(b) on 2026-05-14 verified this), with a per-model rate-card fallback
    so the budget guard never silently fails open.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class InsightsResponse(BaseModel):
    content: str          # raw JSON string from LLM
    input_tokens: int
    output_tokens: int
    cost_usd: float


class InsightsError(Exception):
    pass


class RateLimitedError(InsightsError):
    """OpenRouter 429. Cron handles separately by aborting the whole run."""

    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__(f"OpenRouter 429 (retry_after={retry_after_s})")
        self.retry_after_s = retry_after_s


# Per-1K-token rates (USD). VERIFIED Task 0(b) on 2026-05-14 via curl against
# OpenRouter. Rates rounded conservatively UP so the rolling-30d budget guard
# trips BEFORE overspend, never after. `_default` is a fail-CLOSED conservative
# estimate — large enough that an unknown-model call hits the $8 budget guard
# within a handful of runs rather than silently fail-open.
INSIGHTS_PRICING_PER_1K = {
    "x-ai/grok-4.3":   {"input": 0.0005,  "output": 0.003},   # verified 2026-05-14
    "openai/gpt-5.5":  {"input": 0.005,   "output": 0.030},   # verified 2026-05-14
    "_default":        {"input": 0.10,    "output": 0.40},    # fail-closed sentinel
}


def _estimate_cost_from_tokens(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Fallback per-1K-token cost when OpenRouter doesn't echo ``usage.cost``.

    Falls through to ``_default`` (intentionally expensive) for unknown models so
    the budget guard fires fast rather than silently undercharging.
    """
    rates = INSIGHTS_PRICING_PER_1K.get(model) or INSIGHTS_PRICING_PER_1K["_default"]
    return (input_tokens / 1000.0) * rates["input"] + (
        output_tokens / 1000.0
    ) * rates["output"]


class InsightsClient:
    """OpenRouter caller for weekly insights."""

    def __init__(
        self,
        api_key: str,
        model: str = "x-ai/grok-4.3",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_s: float = 30.0,
        app_title: str = "WisprAlt-Insights",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._app_title = app_title
        # connect=10s leaves 20s read budget against the 30s wall-clock for
        # the typical case; the asyncio.wait_for wrapper enforces the hard cap.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=2),
        )
        logger.info(
            "InsightsClient initialized model=%s timeout_s=%.1f", model, timeout_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def model(self) -> str:
        """Public read-only access to the configured model name.

        Callers (cron upsert) previously reached into ``_model`` directly;
        this property is the supported surface.
        """
        return self._model

    async def analyze(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
    ) -> InsightsResponse:
        """One LLM round-trip with JSON-object response format."""
        try:
            # `httpx.Timeout(...)` only bounds individual phases (connect,
            # read, write, pool) — NOT a wall-clock budget. Wrap in
            # asyncio.wait_for so the total request is hard-capped, mirroring
            # mercury_client.MercuryClient.clean_up (see mercury_client.py:188-219).
            r = await asyncio.wait_for(
                self._client.post(
                    "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "X-Title": self._app_title,
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"},
                    },
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise InsightsError(
                f"OpenRouter timed out after {self._timeout_s}s"
            ) from exc

        if r.status_code == 429:
            retry_after_raw = r.headers.get("retry-after", "")
            retry_after: float | None
            if retry_after_raw.replace(".", "", 1).isdigit():
                retry_after = float(retry_after_raw)
            else:
                retry_after = None
            raise RateLimitedError(retry_after_s=retry_after)
        if r.status_code != 200:
            raise InsightsError(f"OpenRouter {r.status_code}: {r.text[:200]}")

        body = r.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {}) or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        # OpenRouter does NOT reliably populate a cost header. Task 0(b) on
        # 2026-05-14 verified the actual key is ``usage.cost`` (NOT
        # ``usage.total_cost`` as some community wrappers claim). Two paths:
        #   1. Read ``usage.cost`` from response body when OpenRouter
        #      provides it (grok-4.3 + gpt-5.5 both do — verified).
        #   2. Fall back to per-1K-token rate hardcoded per model name in
        #      INSIGHTS_PRICING_PER_1K. Unknown model → ``_default`` rate
        #      (intentionally conservative-high) so budget guard NEVER silently
        #      fails open.
        body_cost = usage.get("cost")
        if isinstance(body_cost, (int, float)) and body_cost > 0:
            cost_usd = float(body_cost)
        else:
            cost_usd = _estimate_cost_from_tokens(
                self._model, input_tokens, output_tokens
            )

        return InsightsResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
