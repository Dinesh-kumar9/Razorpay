"""
Application configuration — single source of truth for all env vars.

All external configuration is read through this module. No other file in the
codebase should call os.environ.get() directly; use `get_settings()` instead.

Design notes:
- pydantic-settings BaseSettings reads from environment and .env file automatically.
- load_dotenv() is called once here; callers do not need to call it themselves.
- get_settings() is cached with @lru_cache so the .env file is parsed exactly once
  per process, not once per import or call.
- All fields have safe defaults so the system runs (with template LLM fallback)
  even when no .env file is present.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env before BaseSettings parses env vars.
# override=False: a shell-level empty string (GEMINI_API_KEY="") takes precedence
# over any value in .env — important for CI where we explicitly disable LLM calls.
load_dotenv(override=False)


class Settings(BaseSettings):
    """
    Runtime configuration for Project Meridian.

    All fields are optional with safe defaults. The system degrades gracefully:
    - No GEMINI_API_KEY  → template fallback for all LLM explanations
    - No AUDIT_DB_PATH   → audit.db created in the current working directory
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # ignore unknown env vars (e.g., CI noise)
    )

    # ── LLM ───────────────────────────────────────────────────────────────────
    gemini_api_key: str = Field(
        default="",
        description=(
            "Google Gemini API key. If empty, all LLM calls silently fall back "
            "to the deterministic template explanation. The pipeline never blocks."
        ),
    )
    google_genai_use_vertexai: bool = Field(
        default=False,
        description="Force Vertex AI mode (False = Developer API / API-key mode).",
    )

    # ── API server ─────────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0", description="API bind host.")
    api_port: int = Field(default=8000, description="API bind port.")

    # ── Simulation ─────────────────────────────────────────────────────────────
    simulation_random_seed: int = Field(
        default=42,
        description="Master RNG seed. All derived seeds are offsets from this value.",
    )
    simulation_n_transactions: int = Field(
        default=5000,
        description="Number of synthetic transactions to generate per batch run.",
    )

    # ── Storage ────────────────────────────────────────────────────────────────
    audit_db_path: Path = Field(
        default=Path("audit.db"),
        description="Path to the SQLite audit database file.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance (parsed once per process).

    Use this in application code:
        from config import get_settings
        settings = get_settings()
        key = settings.gemini_api_key

    In tests, override with:
        from unittest.mock import patch
        with patch("config.get_settings", return_value=Settings(gemini_api_key="test-key")):
            ...
    """
    return Settings()
