import asyncio
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.db.models import Team, User
from app.security import utcnow
from app.store import PostgresStore

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


@pytest.fixture
def migrated_postgres_url():
    admin_url = os.getenv(ADMIN_URL_ENV)
    if not admin_url:
        pytest.skip(f"set {ADMIN_URL_ENV} to an admin URL for a disposable PostgreSQL test database")
    parsed = make_url(admin_url)
    if not parsed.database or not parsed.username:
        pytest.fail(f"{ADMIN_URL_ENV} must include an administrative database and role")
    suffix = uuid.uuid4().hex
    role, database, password = (f"company_agent_it_{suffix}", f"company_agent_it_{suffix}", uuid.uuid4().hex)
    qr, qd, qa = _quote_identifier(role), _quote_identifier(database), _quote_identifier(parsed.username)
    target_url = parsed.set(username=role, password=password, database=database).render_as_string(hide_password=False)
    api_root = Path(__file__).parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    old_url = os.environ.get("COMPANY_AGENT_DATABASE_URL")
    role_created = membership_granted = database_created = False
    try:
        asyncio.run(_admin_execute(admin_url, f"CREATE ROLE {qr} LOGIN PASSWORD {_quote_literal(password)}"))
        role_created = True
        asyncio.run(_admin_execute(admin_url, f"GRANT {qr} TO {qa}"))
        membership_granted = True
        asyncio.run(_admin_execute(admin_url, f"CREATE DATABASE {qd} OWNER {qr}"))
        database_created = True
        os.environ["COMPANY_AGENT_DATABASE_URL"] = target_url
        from app.settings import get_settings

        get_settings.cache_clear()
        command.upgrade(config, "head")
        yield target_url
    finally:
        if old_url is None:
            os.environ.pop("COMPANY_AGENT_DATABASE_URL", None)
        else:
            os.environ["COMPANY_AGENT_DATABASE_URL"] = old_url
        from app.settings import get_settings

        get_settings.cache_clear()
        if database_created:
            asyncio.run(
                _admin_execute(
                    admin_url,
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                    database,
                )
            )
            asyncio.run(_admin_execute(admin_url, f"DROP DATABASE {qd}"))
        if membership_granted:
            asyncio.run(_admin_execute(admin_url, f"REVOKE {qr} FROM {qa}"))
        if role_created:
            asyncio.run(_admin_execute(admin_url, f"DROP ROLE {qr}"))


@pytest.mark.integration
def test_real_postgres_store_lifecycle(migrated_postgres_url: str) -> None:
    async def exercise() -> None:
        engine = create_async_engine(migrated_postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                team_a, team_b = (
                    Team(slug=f"a-{uuid.uuid4().hex}", name="A"),
                    Team(slug=f"b-{uuid.uuid4().hex}", name="B"),
                )
                session.add_all([team_a, team_b])
                await session.flush()
                user_a = User(team_id=team_a.id, email="a@example.test", password_hash="hash")
                user_b = User(team_id=team_b.id, email="b@example.test", password_hash="hash")
                session.add_all([user_a, user_b])
                await session.flush()
                team_a_id, team_b_id = team_a.id, team_b.id
                user_a_id, user_b_id = user_a.id, user_b.id
                user_a_email, user_b_email = user_a.email, user_b.email
                await session.commit()
                store = PostgresStore(session)
                identity_a = await store.find_user(team_a_id, user_a_email)
                identity_b = await store.find_user(team_b_id, user_b_email)
                assert identity_a and identity_b
                assert identity_a.id == user_a_id and identity_b.id == user_b_id

                await store.create_session(identity_a, "session-a", utcnow() + timedelta(minutes=5))
                assert (await store.session_user("session-a")).id == identity_a.id
                await store.create_pairing_code(identity_a, "pair-once", utcnow() + timedelta(minutes=5))
                device, credential, projects = await store.consume_pairing_code(
                    "pair-once", "device", "public-key", "runtime", ["Project"]
                )
                with pytest.raises(PermissionError):
                    await store.consume_pairing_code("pair-once", "other", "other-key", "runtime", ["Other"])
                assert await store.authenticate_device(device.id, credential) == device
                assert len(await store.list_projects(identity_a)) == 1
                assert len(await store.list_devices(identity_a)) == 1
                assert await store.list_devices(identity_b) == []

                conversation = await store.create_conversation(identity_a, "Lifecycle")
                task, created = await store.create_task(
                    identity_a, device.id, projects[0].id, conversation.id, "idem", "prompt"
                )
                same, repeated = await store.create_task(
                    identity_a, device.id, projects[0].id, conversation.id, "idem", "prompt"
                )
                assert created and not repeated and same.id == task.id
                with pytest.raises(RuntimeError):
                    await store.create_task(
                        identity_a, device.id, projects[0].id, conversation.id, "competing", "prompt"
                    )
                assert await store.get_task_for_user(task.id, identity_b) is None
                assert await store.acknowledge_delivery(device, task.id, task.delivery_id)

                first = await store.append_event(device, task.id, "event-1", "text.delta", {"text": "x"})
                duplicate = await store.append_event(device, task.id, "event-1", "text.delta", {"text": "x"})
                assert first.sequence == duplicate.sequence == 1
                approval_event = await store.append_event(
                    device,
                    task.id,
                    "approval-1",
                    "item/commandExecution/requestApproval",
                    {"request_id": "request-1"},
                )
                approval = (await store.list_approvals(task.id, identity_a))[0]
                assert approval_event.payload["approval_id"] == str(approval.id)
                decided, changed = await store.decide_approval(approval.id, identity_a, "approved")
                assert changed and decided.decision_delivery_id
                assert await store.acknowledge_approval_decision(device, approval.id, decided.decision_delivery_id)

                token = "model-token"
                authorization = await store.create_model_token(
                    device,
                    task.id,
                    token,
                    f"jti-{uuid.uuid4().hex}",
                    "deepseek-v4-flash",
                    utcnow() + timedelta(minutes=5),
                )
                assert await store.validate_model_token(token, "deepseek-v4-flash") == authorization
                assert await store.record_model_usage(authorization, "provider-request", 7, 3)
                assert not await store.record_model_usage(authorization, "provider-request", 7, 3)
                assert await store.model_usage_total(team_a_id) == 10

                await store.append_event(device, task.id, "complete-1", "turn/completed", {})
                assert await store.validate_model_token(token, "deepseek-v4-flash") is None
                rollback, rollback_created = await store.request_rollback(task.id, identity_a)
                assert rollback_created
                assert await store.acknowledge_rollback(device, task.id, rollback.delivery_id, "succeeded")
                audit_types = {event.event_type for event in await store.task_audit(task.id, identity_a)}
                assert {
                    "task.created",
                    "task.delivery_acknowledged",
                    "approval.approved",
                    "model.token_issued",
                    "codex.turn/completed",
                    "task.rollback_requested",
                    "task.rollback_succeeded",
                } <= audit_types
        finally:
            await engine.dispose()

    asyncio.run(exercise())


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
