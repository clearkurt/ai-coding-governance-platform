from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="COMPANY_AGENT_", extra="ignore")

    database_url: PostgresDsn
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    session_cookie_secure: bool = True
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    responses_max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    responses_timeout_seconds: float = Field(default=120.0, ge=1, le=300)
    responses_max_concurrency: int = Field(default=32, ge=1, le=256)
    model_token_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    model_daily_token_quota_per_team: int = Field(default=1_000_000, ge=1, le=1_000_000_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

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
        if upstream.path not in {"", "/"} or upstream.query or upstream.fragment:
            raise ValueError("production DeepSeek base URL must be an origin without path, query, or fragment")
        key = self.deepseek_api_key or ""
        stripped = key.strip()
        normalized = stripped.lower()
        if (
            key != stripped
            or len(stripped) < 32
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
            or any(marker in normalized for marker in ("replace", "change-me", "placeholder", "example"))
        ):
            raise ValueError("production requires a non-placeholder DeepSeek API key of at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
