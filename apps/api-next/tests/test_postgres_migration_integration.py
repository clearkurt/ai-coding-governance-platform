import asyncio
import json
import os
import socket
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.db.models import TaskEvent, Team, User
from app.device_registry import DeviceConnectionRegistry
from app.main import app, websocket_store
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

            # Simulate process/Store reconstruction after the committed task dispatch.
            async with sessions() as session:
                store = PostgresStore(session)
                pending = await store.pending_tasks_for_device(device)
                assert [item.id for item in pending] == [task.id]
                assert pending[0].delivery_id == task.delivery_id
                assert await store.acknowledge_delivery(device, task.id, task.delivery_id)
                assert (await store.pending_tasks_for_device(device))[0].status == "running"

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
                approval_delivery_id = decided.decision_delivery_id

            # An unacknowledged approval decision must survive Store reconstruction.
            async with sessions() as session:
                store = PostgresStore(session)
                pending_approvals = await store.pending_approval_decisions(device)
                assert len(pending_approvals) == 1
                assert pending_approvals[0].id == approval.id
                assert pending_approvals[0].decision_delivery_id == approval_delivery_id
                assert await store.acknowledge_approval_decision(device, approval.id, approval_delivery_id)

            async with sessions() as session:
                store = PostgresStore(session)
                assert await store.pending_approval_decisions(device) == []

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
                rollback_delivery_id = rollback.delivery_id

            # The terminal task's unacknowledged rollback must also survive reconstruction.
            async with sessions() as session:
                store = PostgresStore(session)
                pending_rollbacks = await store.pending_rollbacks(device)
                assert len(pending_rollbacks) == 1
                assert pending_rollbacks[0].task_id == task.id
                assert pending_rollbacks[0].delivery_id == rollback_delivery_id
                assert await store.acknowledge_rollback(device, task.id, rollback_delivery_id, "succeeded")

            async with sessions() as session:
                store = PostgresStore(session)
                assert await store.pending_rollbacks(device) == []
                assert await store.pending_tasks_for_device(device) == []
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


@pytest.mark.integration
def test_localhost_websocket_reconnect_replays_and_deduplicates(migrated_postgres_url: str) -> None:
    async def exercise() -> None:
        import uvicorn
        import websockets

        engine = create_async_engine(migrated_postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            team = Team(slug=f"socket-{uuid.uuid4().hex}", name="Socket")
            session.add(team)
            await session.flush()
            user = User(team_id=team.id, email="socket@example.test", password_hash="hash")
            session.add(user)
            await session.flush()
            team_id, user_email = team.id, user.email
            await session.commit()
            store = PostgresStore(session)
            identity = await store.find_user(team_id, user_email)
            await store.create_pairing_code(identity, "socket-pair", utcnow() + timedelta(minutes=5))
            device, credential, projects = await store.consume_pairing_code(
                "socket-pair", "socket-device", "socket-key", "runtime", ["Socket project"]
            )
            conversation = await store.create_conversation(identity, "Socket lifecycle")
            task, _ = await store.create_task(
                identity, device.id, projects[0].id, conversation.id, "socket-task", "prompt"
            )

        async def override_store():
            return PostgresStore(sessions())

        old_registry = app.state.device_registry
        app.state.device_registry = DeviceConnectionRegistry()
        app.dependency_overrides[websocket_store] = override_store
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            uri = f"ws://127.0.0.1:{port}/ws/devices"
            auth = {"type": "authenticate", "device_id": str(device.id), "credential": credential}

            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                assert json.loads(await ws.recv())["type"] == "authenticated"
                first_delivery = json.loads(await ws.recv())
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                await ws.recv()
                replay = json.loads(await ws.recv())
                assert replay["task_id"] == first_delivery["task_id"]
                assert replay["delivery_id"] == first_delivery["delivery_id"]
                await ws.send(
                    json.dumps({"type": "task.dispatch.ack", "task_id": str(task.id), "delivery_id": task.delivery_id})
                )
                assert json.loads(await ws.recv())["accepted"] is True
                event = {
                    "type": "task.event",
                    "task_id": str(task.id),
                    "source_event_id": "socket-event",
                    "event_type": "text.delta",
                    "payload": {"text": "x"},
                }
                await ws.send(json.dumps(event))
                await asyncio.sleep(0.05)
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps(event))
                event_ack = json.loads(await ws.recv())
                assert event_ack["source_event_id"] == "socket-event" and event_ack["sequence"] == 1

            async with sessions() as session:
                store = PostgresStore(session)
                assert (
                    await session.scalar(
                        select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == task.id)
                    )
                    == 1
                )
                approval_event = await store.append_event(
                    device, task.id, "socket-approval", "item/commandExecution/requestApproval", {"request_id": 42}
                )
                approval = (await store.list_approvals(task.id, identity))[0]
                decided, _ = await store.decide_approval(approval.id, identity, "approved")
                assert approval_event.sequence == 2
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                await ws.recv()
                await ws.recv()
                approval_delivery = json.loads(await ws.recv())
                assert approval_delivery["delivery_id"] == decided.decision_delivery_id
                await ws.send(
                    json.dumps(
                        {
                            "type": "approval.decision.ack",
                            "approval_id": str(approval.id),
                            "delivery_id": decided.decision_delivery_id,
                        }
                    )
                )
                assert json.loads(await ws.recv())["accepted"] is True

            async with sessions() as session:
                store = PostgresStore(session)
                await store.append_event(device, task.id, "socket-complete", "turn/completed", {})
                rollback, _ = await store.request_rollback(task.id, identity)
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                await ws.recv()
                rollback_delivery = json.loads(await ws.recv())
                assert rollback_delivery["delivery_id"] == rollback.delivery_id
                await ws.send(
                    json.dumps(
                        {
                            "type": "task.rollback.ack",
                            "task_id": str(task.id),
                            "delivery_id": rollback.delivery_id,
                            "status": "succeeded",
                        }
                    )
                )
                assert json.loads(await ws.recv())["accepted"] is True
        finally:
            server.should_exit = True
            await server_task
            app.dependency_overrides.pop(websocket_store, None)
            app.state.device_registry = old_registry
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_uvicorn_process_restart_replays_unacknowledged_delivery(migrated_postgres_url: str) -> None:
    async def exercise() -> None:
        import websockets

        engine = create_async_engine(migrated_postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            team = Team(slug=f"restart-{uuid.uuid4().hex}", name="Restart")
            session.add(team)
            await session.flush()
            user = User(team_id=team.id, email="restart@example.test", password_hash="hash")
            session.add(user)
            await session.flush()
            team_id, user_email = team.id, user.email
            await session.commit()
            store = PostgresStore(session)
            identity = await store.find_user(team_id, user_email)
            await store.create_pairing_code(identity, "restart-pair", utcnow() + timedelta(minutes=5))
            device, credential, projects = await store.consume_pairing_code(
                "restart-pair", "restart-device", "restart-key", "runtime", ["Restart project"]
            )
            conversation = await store.create_conversation(identity, "Restart conversation")
            task, _ = await store.create_task(
                identity, device.id, projects[0].id, conversation.id, "restart-task", "prompt"
            )

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        api_root = Path(__file__).parents[1]
        environment = os.environ.copy()
        environment["COMPANY_AGENT_DATABASE_URL"] = migrated_postgres_url

        async def start_server():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                cwd=api_root,
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(200):
                if process.returncode is not None:
                    raise RuntimeError("uvicorn child exited during startup")
                try:
                    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    return process
                except OSError:
                    await asyncio.sleep(0.01)
            process.terminate()
            await process.wait()
            raise TimeoutError("uvicorn child did not start")

        async def stop_server(process):
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)

        uri = f"ws://127.0.0.1:{port}/ws/devices"
        auth = {"type": "authenticate", "device_id": str(device.id), "credential": credential}
        first_process = await start_server()
        second_process = None
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                assert json.loads(await ws.recv())["type"] == "authenticated"
                first_delivery = json.loads(await ws.recv())
            await stop_server(first_process)

            second_process = await start_server()
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps(auth))
                assert json.loads(await ws.recv())["type"] == "authenticated"
                replay = json.loads(await ws.recv())
                assert replay["task_id"] == first_delivery["task_id"] == str(task.id)
                assert replay["delivery_id"] == first_delivery["delivery_id"] == task.delivery_id
                await ws.send(
                    json.dumps(
                        {
                            "type": "task.dispatch.ack",
                            "task_id": str(task.id),
                            "delivery_id": task.delivery_id,
                        }
                    )
                )
                assert json.loads(await ws.recv())["accepted"] is True
        finally:
            await stop_server(first_process)
            if second_process:
                await stop_server(second_process)
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
