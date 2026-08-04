from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app import release_check
from app.db.schema import TARGET_SCHEMA_REVISION, has_target_schema
from app.main import ready


class ScalarResult:
    def __init__(self, revisions: list[str]):
        self.revisions = revisions

    def scalars(self):
        return iter(self.revisions)


class FakeSession:
    def __init__(self, revisions: list[str] | None = None, error: Exception | None = None):
        self.revisions = revisions or []
        self.error = error

    async def execute(self, _statement):
        if self.error:
            raise self.error
        return ScalarResult(self.revisions)


@pytest.mark.asyncio
@pytest.mark.parametrize("revisions", [[], ["old"], [TARGET_SCHEMA_REVISION, "other"]])
async def test_schema_contract_rejects_empty_wrong_or_multiple_revisions(revisions: list[str]) -> None:
    assert not await has_target_schema(FakeSession(revisions))


@pytest.mark.asyncio
async def test_schema_contract_accepts_exact_target_revision() -> None:
    assert await has_target_schema(FakeSession([TARGET_SCHEMA_REVISION]))
    assert await ready(FakeSession([TARGET_SCHEMA_REVISION])) == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [FakeSession([]), FakeSession(error=RuntimeError("secret database error"))])
async def test_readiness_failure_is_fixed_and_non_sensitive(session: FakeSession) -> None:
    with pytest.raises(HTTPException) as error:
        await ready(session)

    assert error.value.status_code == 503
    assert error.value.detail == "service unavailable"
    assert "secret" not in error.value.detail


def test_alembic_has_one_head_matching_runtime_contract() -> None:
    api_root = Path(__file__).parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))

    assert ScriptDirectory.from_config(config).get_heads() == [TARGET_SCHEMA_REVISION]


@pytest.mark.parametrize(("schema_ready", "exit_code"), [(True, 0), (False, 1)])
def test_release_check_reports_only_supported_schema_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schema_ready: bool,
    exit_code: int,
) -> None:
    async def fake_check(_settings) -> bool:
        return schema_ready

    monkeypatch.setattr(release_check, "validate_configuration", lambda: (True, []))
    monkeypatch.setattr(release_check, "Settings", lambda: object())
    monkeypatch.setattr(release_check, "check_online_schema", fake_check)

    assert release_check.main() == exit_code
    assert ("release check passed" if schema_ready else "database schema unsupported") in capsys.readouterr().out


def test_release_check_database_error_does_not_leak_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_check(_settings) -> bool:
        raise SQLAlchemyError("postgresql://user:secret@db/private")

    monkeypatch.setattr(release_check, "validate_configuration", lambda: (True, []))
    monkeypatch.setattr(release_check, "Settings", lambda: object())
    monkeypatch.setattr(release_check, "check_online_schema", fail_check)

    assert release_check.main() == 1
    output = capsys.readouterr().out
    assert output == "release check failed: database unavailable\n"
    assert "secret" not in output
