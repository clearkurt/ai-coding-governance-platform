import asyncio
import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command

ADMIN_URL_ENV = "COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL"
EXPECTED_TASK_STATUSES = {
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
}


def test_postgres_identifier_quoting_escapes_role_names() -> None:
    assert _quote_identifier('admin"name') == '"admin""name"'
    with pytest.raises(ValueError):
        _quote_identifier("admin\x00name")


def _asyncpg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _quote_identifier(value: str) -> str:
    if "\x00" in value:
        raise ValueError("PostgreSQL identifier contains a null byte")
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("PostgreSQL literal contains a null byte")
    return "'" + value.replace("'", "''") + "'"


async def _admin_execute(admin_url: str, statement: str, *arguments: object) -> None:
    import asyncpg

    connection = await asyncpg.connect(_asyncpg_url(admin_url))
    try:
        await connection.execute(statement, *arguments)
    finally:
        await connection.close()


async def _inspect_database(database_url: str) -> tuple[set[str], int, int, set[str]]:
    import asyncpg

    connection = await asyncpg.connect(_asyncpg_url(database_url))
    try:
        tables = set(
            await connection.fetchval(
                "SELECT array_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public'"
            )
            or []
        )
        foreign_keys = await connection.fetchval(
            "SELECT count(*) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' AND c.contype = 'f'"
        )
        unique_constraints = await connection.fetchval(
            "SELECT count(*) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' AND c.contype = 'u'"
        )
        enum_values = set(
            await connection.fetchval(
                "SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' AND t.typname = 'task_status'"
            )
            or []
        )
        return tables, foreign_keys, unique_constraints, enum_values
    finally:
        await connection.close()


@pytest.mark.integration
def test_real_postgres_upgrade_downgrade_and_reupgrade() -> None:
    admin_url = os.getenv(ADMIN_URL_ENV)
    if not admin_url:
        pytest.skip(f"set {ADMIN_URL_ENV} to an admin URL for a disposable PostgreSQL test database")

    suffix = uuid.uuid4().hex
    role = f"company_agent_it_{suffix}"
    database = f"company_agent_it_{suffix}"
    password = uuid.uuid4().hex
    parsed_admin_url = make_url(admin_url)
    if not parsed_admin_url.database:
        pytest.fail(f"{ADMIN_URL_ENV} must name an existing administrative database")
    if not parsed_admin_url.username:
        pytest.fail(f"{ADMIN_URL_ENV} must include the administrative role name")
    quoted_admin = _quote_identifier(parsed_admin_url.username)
    quoted_role = _quote_identifier(role)
    quoted_database = _quote_identifier(database)
    target_url = parsed_admin_url.set(username=role, password=password, database=database).render_as_string(
        hide_password=False
    )
    api_root = Path(__file__).parents[1]
    alembic_config = Config(str(api_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(api_root / "alembic"))
    previous_database_url = os.environ.get("COMPANY_AGENT_DATABASE_URL")
    role_created = False
    membership_granted = False
    database_created = False

    try:
        asyncio.run(_admin_execute(admin_url, f"CREATE ROLE {quoted_role} LOGIN PASSWORD {_quote_literal(password)}"))
        role_created = True
        asyncio.run(_admin_execute(admin_url, f"GRANT {quoted_role} TO {quoted_admin}"))
        membership_granted = True
        asyncio.run(_admin_execute(admin_url, f"CREATE DATABASE {quoted_database} OWNER {quoted_role}"))
        database_created = True
        os.environ["COMPANY_AGENT_DATABASE_URL"] = target_url
        from app.settings import get_settings

        get_settings.cache_clear()

        command.upgrade(alembic_config, "head")
        tables, foreign_keys, unique_constraints, enum_values = asyncio.run(_inspect_database(target_url))
        assert len(tables - {"alembic_version"}) == 18
        assert "alembic_version" in tables
        assert foreign_keys == 21
        assert unique_constraints == 26
        assert enum_values == EXPECTED_TASK_STATUSES

        command.downgrade(alembic_config, "base")
        tables, foreign_keys, unique_constraints, enum_values = asyncio.run(_inspect_database(target_url))
        assert tables == {"alembic_version"}
        assert foreign_keys == 0
        assert unique_constraints == 0
        assert enum_values == set()

        command.upgrade(alembic_config, "head")
        tables, foreign_keys, unique_constraints, enum_values = asyncio.run(_inspect_database(target_url))
        assert len(tables - {"alembic_version"}) == 18
        assert foreign_keys == 21
        assert unique_constraints == 26
        assert enum_values == EXPECTED_TASK_STATUSES
    finally:
        if previous_database_url is None:
            os.environ.pop("COMPANY_AGENT_DATABASE_URL", None)
        else:
            os.environ["COMPANY_AGENT_DATABASE_URL"] = previous_database_url
        from app.settings import get_settings

        get_settings.cache_clear()
        if database_created:
            asyncio.run(
                _admin_execute(
                    admin_url,
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    database,
                )
            )
            asyncio.run(_admin_execute(admin_url, f"DROP DATABASE {quoted_database}"))
        if membership_granted:
            asyncio.run(_admin_execute(admin_url, f"REVOKE {quoted_role} FROM {quoted_admin}"))
        if role_created:
            asyncio.run(_admin_execute(admin_url, f"DROP ROLE {quoted_role}"))
