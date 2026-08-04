import os

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
