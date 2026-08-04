from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPANY_AGENT_", extra="ignore")

    database_url: PostgresDsn
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    session_cookie_secure: bool = True
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    responses_max_body_bytes: int = 2 * 1024 * 1024
    responses_timeout_seconds: float = 120.0
    responses_max_concurrency: int = 32
    model_token_ttl_seconds: int = 300
    model_daily_token_quota_per_team: int = 1_000_000

    @model_validator(mode="after")
    def validate_deployment_boundary(self) -> "Settings":
        upstream = urlsplit(self.deepseek_base_url)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname or upstream.username or upstream.password:
            raise ValueError("DeepSeek base URL must be an HTTP(S) origin without embedded credentials")
        if self.environment != "production":
            return self
        if not self.session_cookie_secure:
            raise ValueError("production requires secure session cookies")
        if upstream.scheme != "https":
            raise ValueError("production requires an HTTPS DeepSeek base URL")
        key = self.deepseek_api_key or ""
        normalized = key.strip().lower()
        if len(key) < 32 or any(marker in normalized for marker in ("replace", "change-me", "placeholder", "example")):
            raise ValueError("production requires a non-placeholder DeepSeek API key of at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
