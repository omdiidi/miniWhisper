"""
config.py — Pydantic Settings for WisprAlt server.

Reads from a .env file at startup (or from real environment variables).
Exposes a module-level ``settings`` singleton used throughout the package.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Re-export verify_env_perms from ops.env_writer so callers that import it
# from config continue to work without a duplicate definition.
from wispralt_server.ops.env_writer import verify_env_perms as verify_env_perms  # noqa: F401

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Secrets — never logged or serialized
    hf_token: SecretStr
    wispralt_api_key: SecretStr

    # Supabase Postgres URL for multi-tenant auth + usage events.
    # Optional: when None, the server runs in break-glass-only mode
    # (env-var WISPRALT_API_KEY grants admin; no per-user lookups).
    supabase_database_url: SecretStr | None = None

    # Server identity
    server_url: str

    # Filesystem paths — resolved to absolute on access
    meeting_output_dir: Path
    job_db_path: Path
    staging_dir: Path

    # Audio analysis
    silence_threshold: float = 0.002

    # Upload guard (default 2 GiB)
    max_upload_bytes: int = 2_147_483_648

    # Rate-limit values (M6) — configurable from .env
    dictate_rate_per_min: int = 60
    meeting_rate_per_hour: int = 4

    # Idle-eviction for meeting models. After this many seconds with no meeting
    # activity (no submission, no in-flight job), unload mlx-whisper + Pyannote
    # AND call mx.metal.clear_cache() to actually return MLX's unified-memory
    # pool to the OS (drops RSS from ~6-9 GB to ~3 GB). Next meeting pays the
    # cold-load cost again (~3s kernel recompile + weight load).
    # Set to 0 to disable eviction (models stay warm forever — old behavior).
    # Default 60s (1 min) — aggressive because meetings are infrequent on
    # this deployment; raise to 300+ if you do meetings in clusters.
    meeting_idle_eviction_seconds: int = 60

    # Hard cap on dictation audio length. Defends against decode-amplification
    # attacks (1KB body that decodes to many minutes) and single-thread executor
    # starvation. Default 300s (5 min) — covers realistic long-form dictations
    # without letting a runaway upload pin the executor.
    dictation_max_duration_s: int = 300

    # Trust CF-Connecting-IP / X-Forwarded-For headers for rate-limit IP extraction.
    # Set to False if exposing FastAPI directly without Cloudflare Tunnel (e.g. LAN testing)
    # to avoid IP spoofing in rate limiting.
    trust_forwarded_headers: bool = True

    # OpenRouter Mercury 2 — server-side smart-formatting post-processor.
    # Optional: when openrouter_api_key is None, smart formatting is silently disabled
    # (clients toggling the X-Smart-Format header on get raw text back).
    openrouter_api_key: str | None = None
    openrouter_model: str = "inception/mercury-2"
    # 600 ms ceiling for the Mercury cleanup (lowered from 1500 ms on
    # 2026-05-02 after measuring 7-day production latency: dictate p95
    # ran 5.4s with the old budget; this caps the disaster ceiling and
    # keeps the path fail-soft). Cross-region OpenRouter on a warm TCP
    # session is 200-400 ms; 600 ms preserves headroom while ruling out
    # the "1.5s waiting on Mercury" failure mode in the long tail.
    # If a deployment sees too many fail-overs to raw text, raise via env
    # (OPENROUTER_TIMEOUT_MS=1000) and revisit.
    openrouter_timeout_ms: int = 600
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_title: str = "WisprAlt"

    # Minimum dictation length (words) before smart-formatting is invoked. Below
    # this, raw Parakeet output is returned unchanged — short utterances aren't
    # worth the LLM round-trip and Parakeet's inline punctuation is good enough.
    #
    # 2026-05-13 bump 100 -> 300: with the OpenRouter timeout pinned at 600 ms
    # (see openrouter_timeout_ms above), Mercury empirically can't complete a
    # 100-300 word polish within budget. Those requests time out and fail-soft
    # to raw Parakeet text anyway, costing the user ~600 ms of dead wait. At
    # >=300 words the cleanup is both noticeable AND inside budget. The user
    # still gets correct Parakeet output (with its inline punctuation) below
    # the threshold — no transcription content is lost, only the polish step.
    smart_format_min_words: int = 300


# Module-level singleton — import as ``from wispralt_server.config import settings``
settings = Settings()
