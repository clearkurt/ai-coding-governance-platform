import os
import uuid

import pytest
from pydantic import ValidationError

from app.preflight import main
from app.settings import Settings

VALID_PRODUCTION = {
    "database_url": "postgresql+asyncpg://agent:test@db.internal:5432/agent",
    "environment": "production",
    "session_cookie_secure": True,
    "deepseek_api_key": "production-secret-value-that-is-long-enough",
    "deepseek_base_url": "https://api.deepseek.com",
}


def test_production_settings_accept_secure_configuration() -> None:
    settings = Settings(**VALID_PRODUCTION)

    assert settings.environment == "production"


@pytest.mark.parametrize(
    "override",
    [
        {"session_cookie_secure": False},
        {"deepseek_base_url": "http://api.deepseek.com"},
        {"deepseek_api_key": None},
        {"deepseek_api_key": "short"},
        {"deepseek_api_key": "replace-with-server-only-key-that-is-long"},
    ],
)
def test_production_settings_reject_insecure_configuration(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | override))


@pytest.mark.parametrize(
    "key",
    [
        " " * 32,
        " valid-production-secret-value-that-is-long-enough",
        "valid-production-secret-value-that-is-long-enough\n",
    ],
)
def test_production_settings_reject_whitespace_or_control_character_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {"deepseek_api_key": key}))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com?region=test",
        "https://api.deepseek.com#fragment",
    ],
)
def test_production_settings_require_unambiguous_upstream_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {"deepseek_base_url": url}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("responses_max_body_bytes", 0),
        ("responses_max_body_bytes", 16 * 1024 * 1024 + 1),
        ("responses_timeout_seconds", 0),
        ("responses_timeout_seconds", 301),
        ("responses_max_concurrency", 0),
        ("responses_max_concurrency", 257),
        ("model_token_ttl_seconds", 0),
        ("model_token_ttl_seconds", 3601),
        ("model_daily_token_quota_per_team", 0),
        ("model_daily_token_quota_per_team", 1_000_000_001),
    ],
)
def test_settings_reject_unsafe_resource_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {field: value}))


def test_log_level_is_normalized_and_restricted() -> None:
    assert Settings(**(VALID_PRODUCTION | {"log_level": "warning"})).log_level == "WARNING"
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {"log_level": "verbose"}))


def test_production_rollout_defaults_disabled_and_allowlist_is_strict() -> None:
    device = uuid.uuid4()
    assert not Settings(**VALID_PRODUCTION).allows_new_codex_task(device)
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {"codex_rollout_mode": "allowlist"}))

    allowlisted = Settings(
        **(VALID_PRODUCTION | {"codex_rollout_mode": "allowlist", "codex_rollout_device_ids": [device, device]})
    )
    assert allowlisted.codex_rollout_device_ids == {device}
    assert allowlisted.allows_new_codex_task(device)
    assert not allowlisted.allows_new_codex_task(uuid.uuid4())
    assert Settings(**(VALID_PRODUCTION | {"codex_rollout_mode": "all"})).allows_new_codex_task(device)


def test_rollout_allowlist_has_bounded_size() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **(
                VALID_PRODUCTION
                | {
                    "codex_rollout_mode": "allowlist",
                    "codex_rollout_device_ids": [uuid.uuid4() for _ in range(1001)],
                }
            )
        )


def test_settings_reject_non_postgres_database() -> None:
    with pytest.raises(ValidationError):
        Settings(**(VALID_PRODUCTION | {"database_url": "sqlite:///agent.db"}))


def test_development_exceptions_are_explicit() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://agent:test@localhost:5432/agent",
        environment="development",
        session_cookie_secure=False,
        deepseek_api_key=None,
        deepseek_base_url="http://localhost:9000",
    )

    assert settings.environment == "development"


def test_preflight_does_not_print_secret(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "replace-with-sensitive-value-that-must-not-print"
    environment = {
        "COMPANY_AGENT_DATABASE_URL": "postgresql+asyncpg://agent:test@db.internal:5432/agent",
        "COMPANY_AGENT_ENVIRONMENT": "production",
        "COMPANY_AGENT_SESSION_COOKIE_SECURE": "true",
        "COMPANY_AGENT_DEEPSEEK_API_KEY": secret,
        "COMPANY_AGENT_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    }
    for name in list(os.environ):
        if name.startswith("COMPANY_AGENT_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert main() == 1
    output = capsys.readouterr().out
    assert secret not in output
    assert "preflight failed" in output
