from functools import lru_cache

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPANY_AGENT_", extra="ignore")

    database_url: PostgresDsn
    environment: str = "development"
    log_level: str = "INFO"
    session_cookie_secure: bool = True
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    responses_max_body_bytes: int = 2 * 1024 * 1024
    responses_timeout_seconds: float = 120.0
    responses_max_concurrency: int = 32
    model_token_ttl_seconds: int = 300
    model_daily_token_quota_per_team: int = 1_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
