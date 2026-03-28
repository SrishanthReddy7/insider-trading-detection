from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.keys", "backend/.env", "backend/.env.keys"),
        extra="ignore",
    )

    # SQLite is the local default; override with DATABASE_URL when needed.
    database_url: str = Field(default="sqlite:///./mnpi_guard.db", validation_alias="DATABASE_URL")
    cors_origins: str = Field(default="*", validation_alias="CORS_ORIGINS")
    storage_dir: str = Field(default="./storage", validation_alias="STORAGE_DIR")

    correlation_window_hours: int = 168  # 7 days
    mnpi_restrict_threshold: int = 75
    alert_threshold: int = 75
    correlation_alert_threshold: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()

