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
    # activity (no submission, no in-flight job), unload WhisperX + Pyannote to
    # reclaim ~2-3 GB python RSS. Next meeting pays the cold-load cost again.
    # Set to 0 to disable eviction (models stay warm forever — old behavior).
    # Default 300s (5 min) — short enough to free RAM in idle workdays, long
    # enough not to thrash mid-cluster of meetings.
    meeting_idle_eviction_seconds: int = 300

    # Trust CF-Connecting-IP / X-Forwarded-For headers for rate-limit IP extraction.
    # Set to False if exposing FastAPI directly without Cloudflare Tunnel (e.g. LAN testing)
    # to avoid IP spoofing in rate limiting.
    trust_forwarded_headers: bool = True

    # OpenRouter Mercury 2 — server-side smart-formatting post-processor.
    # Optional: when openrouter_api_key is None, smart formatting is silently disabled
    # (clients toggling the X-Smart-Format header on get raw text back).
    openrouter_api_key: str | None = None
    openrouter_model: str = "inception/mercury-2"
    # 1500 ms (NOT 250 ms): cross-region OpenRouter calls + TLS handshake on cold
    # connections regularly exceed 250 ms. 1500 ms is the practical upper bound to
    # avoid silent fall-through to raw on cold connections.
    openrouter_timeout_ms: int = 1500
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_title: str = "WisprAlt"


# Module-level singleton — import as ``from wispralt_server.config import settings``
settings = Settings()
