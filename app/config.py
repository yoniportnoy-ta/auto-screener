"""Configuration via Pydantic settings — loaded from env vars + .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. All values come from environment variables.

    On Render the values come from the service's env-var dashboard. Locally
    they come from `.env` (see `.env.example` for the full list).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── App ─────────────────────────────────────────────────────────────────
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    port: int = 8000
    screener_api_token: str = Field(default="changeme", description="Shared secret for write endpoints")

    # ─── Database ────────────────────────────────────────────────────────────
    database_url: str = Field(default="postgresql://localhost/auto_screener_dev")

    # ─── Comeet — public API ─────────────────────────────────────────────────
    comeet_api_key: str = ""
    comeet_api_secret: str = ""
    comeet_base_url: str = "https://api.comeet.co"

    # ─── Comeet — internal API ───────────────────────────────────────────────
    comeet_app_email: str = ""
    comeet_app_password: str = ""
    comeet_app_base_url: str = "https://app.comeet.co"
    captcha_api_key: str = ""
    recaptcha_site_key: str = "6LezYW4sAAAAAFyQqneztlSf4Fj_pxDeJDDYxYP7"
    recaptcha_action: str = "login"
    recaptcha_min_score: float = 0.3

    # ─── Anthropic ───────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"

    # ─── Scoring tuning ──────────────────────────────────────────────────────
    scoring_use_v2: bool = True
    note_rating_threshold: int = 3
    auto_tag_enabled: bool = False
    tag_rating_threshold: int = 3
    screener_max_per_run: int = 20
    screener_step_substrings: str = "cv screen / recruiter"
    excluded_recruiting_statuses: str = "Rejected,Withdrawn,Hired"

    # Calibration
    calibration_min_samples: int = 5
    calibration_max_delta: float = 1.0
    calibration_min_abs_delta: float = 0.3
    learned_rubric_min_samples: int = 5

    # ─── Helpers ─────────────────────────────────────────────────────────────
    @property
    def step_substrings_list(self) -> list[str]:
        return [s.strip().lower() for s in self.screener_step_substrings.split(",") if s.strip()]

    @property
    def excluded_statuses_list(self) -> list[str]:
        raw = self.excluded_recruiting_statuses.strip()
        if raw == "-":
            return []
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Use `from app.config import get_settings` everywhere."""
    return Settings()


# Convenience export so callers can `from app.config import settings` if they prefer.
settings = get_settings()
