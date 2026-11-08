"""
config.py — Pydantic Settings for WisprAlt server.

Reads from a .env file at startup (or from real environment variables).
Exposes a module-level ``settings`` singleton used throughout the package.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Re-export verify_env_perms from ops.env_writer so callers that import it
# from config continue to work without a duplicate definition.
from wispralt.ops.env_writer import verify_env_perms as verify_env_perms  # noqa: F401

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server identity
    server_url: str

    # Local-mode data root: wispralt.db, outputs/ and staging/ live here.
    # Required and absolute (an empty value would mean cwd). `wispralt setup`
    # writes it into .env; the server never creates it (setup does).
    data_dir: Path

    # Upload guard (default 2 GiB)
    max_upload_bytes: int = 2_147_483_648

    # Rate-limit values (M6) — configurable from .env
    dictate_rate_per_min: int = 60

    # Idle-eviction for the file transcription model. After this many seconds
    # with no file-job activity (no in-flight job), unload mlx-whisper AND call
    # mx.clear_cache() to actually return MLX's unified-memory pool to the OS
    # (drops RSS from ~6-9 GB to ~3 GB). The next file job pays the cold-load
    # cost again (~3s kernel recompile + weight load).
    # Set to 0 to disable eviction (model stays warm forever — old behavior).
    # Default 60s (1 min) — aggressive because file jobs are infrequent on
    # this deployment; raise to 300+ if you transcribe files in clusters.
    file_model_idle_eviction_seconds: int = 60

    # Eviction self-check thresholds (added by the 2026-05-19 invariant-gates plan).
    # Probe-verified 2026-05-19 that mx.metal.get_active_memory() is synchronous
    # with mx.clear_cache() — no async sampling needed.
    file_eviction_delta_floor_mb: int = 1000
    # A real eviction releases ~1500 MB of file-model (Whisper) active memory.
    # Today's bug had delta=0. Floor of 1000 catches the bug AND partial-release
    # regressions (e.g., a future library leaking 500 MB while clearing 1000).

    file_eviction_pre_active_gate_mb: int = 1500
    # Parakeet baseline (~1218) + 300 MB headroom = definitively file-model-loaded.
    # Excludes cold-start, partial-load failures. Gate prevents false-positives
    # when there was nothing meaningful to evict.

    # Hard cap on dictation audio length. Defends against decode-amplification
    # attacks (1KB body that decodes to many minutes) and single-thread executor
    # starvation. Default 300s (5 min) — covers realistic long-form dictations
    # without letting a runaway upload pin the executor.
    dictation_max_duration_s: int = 900   # v0.4.3: raised 300→900s (15 min). Supports the 10-min+ recordings the user wants. Streaming offloads inference during recording so end-to-end finalize stays fast even on long dictations; the fallback path is brutally slow at 10 min (single Parakeet pass ≈3 min) but that's only on the failure ladder, not the normal path.

    # Streaming dictation (cut-on-silence chunked transcription). Phase 1, opt-in.
    # streaming_session_ttl_s: how long an idle streaming session lives before
    #   the sweeper aborts it (matches the 15-min Phase 0 worst-case dictation).
    # streaming_max_active: server-wide cap on concurrent streaming sessions —
    #   protects the single-thread Parakeet executor from queue depth blow-up.
    # streaming_max_queue_depth: per-session pending chunk cap (excess → 429).
    # streaming_finalize_timeout_s: max wait inside /finalize for pending +
    #   tail inference to complete before raising FinalizeTimeout.
    streaming_session_ttl_s: int = 900
    streaming_max_active: int = 2
    streaming_max_queue_depth: int = 12   # v0.4.2: raised 6→12 for headroom with 5 s chunks (route now counts only in-flight tasks)
    streaming_finalize_timeout_s: int = 15

    # Trust CF-Connecting-IP / X-Forwarded-For headers for rate-limit IP extraction.
    # Set to False if exposing FastAPI directly without Cloudflare Tunnel (e.g. LAN testing)
    # to avoid IP spoofing in rate limiting.
    trust_forwarded_headers: bool = True

    # OpenRouter key for the weekly insights cron (InsightsClient). Optional:
    # when None, the cron is a no-op.
    openrouter_api_key: str | None = None

    # Application log (RotatingFileHandler). launchd's server.log /
    # server.err.log are left for crash output only. /admin/server-log reads
    # this file. 10 MB x (1 + 5 backups) = ~60 MB cap.
    server_log_path: Path = Path.home() / "Library" / "Logs" / "WisprAlt" / "wispralt.log"
    server_log_max_bytes: int = 10 * 1024 * 1024
    server_log_backup_count: int = 5

    # Plan A /me/history pagination size. Each request returns at most this
    # many rows per leg (dictations + meetings), merged and capped to this
    # value before render. Load-more uses per-leg cursors.
    history_page_size: int = 50

    # Weekly AI insights (opt-in). Runtime settings — enabled + disclosure, model, 30-day
    # cost cap, timezone — live in the DB ``settings`` table and are edited at
    # /admin/settings; these are the defaults it falls back to. ``insights_model`` was
    # verified on OpenRouter 2026-05-14 (about $0.65/week at our size).
    insights_model: str = "x-ai/grok-4.3"
    insights_timeout_s: float = 30.0
    insights_input_word_cap: int = 30000
    insights_per_person_min_dictations: int = 5
    # None → the host's zone (/etc/localtime), else UTC. The DB setting wins over both.
    insights_timezone: str | None = None

    @field_validator("data_dir", mode="before")
    @classmethod
    def _absolute_data_dir(cls, value: object) -> object:
        if value is None or not str(value).strip() or not Path(str(value)).is_absolute():
            raise ValueError("DATA_DIR must be an absolute path")
        return Path(str(value))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wispralt.db"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"


# Module-level singleton — import as ``from wispralt.config import settings``
settings = Settings()
