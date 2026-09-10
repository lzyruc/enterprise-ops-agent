from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    agent_mode: str = "demo"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    database_path: Path = Path("data/enterprise_agent.db")
    max_agent_steps: int = Field(default=8, ge=2, le=20)
    tool_timeout_seconds: float = Field(default=15.0, ge=1, le=60)
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"
    api_key: str = "change-me-in-production"

    @property
    def cors_origins(self) -> list[str]:
        return [value.strip() for value in self.allowed_origins.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
