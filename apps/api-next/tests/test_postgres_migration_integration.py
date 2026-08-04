import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import pytest
from alembic.config import Config
from asyncpg import PostgresError
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


def _postgres_tool(name: str) -> list[str] | None:
    prefix_raw = os.getenv("COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX")
    if prefix_raw:
        try:
            prefix = json.loads(prefix_raw)
        except json.JSONDecodeError as error:
            raise ValueError("COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX must be a JSON string array") from error
        if not isinstance(prefix, list) or not prefix or not all(isinstance(item, str) and item for item in prefix):
            raise ValueError("COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX must be a non-empty JSON string array")
        return [*prefix, name]
    explicit = os.getenv(f"COMPANY_AGENT_TEST_{name.upper()}")
    if explicit:
        return [explicit] if Path(explicit).is_file() else None
    discovered = shutil.which(name)
    if discovered:
        return [discovered]
    program_files = Path(os.getenv("ProgramFiles", "C:/Program Files")) / "PostgreSQL"
    candidates = sorted(program_files.glob(f"*/bin/{name}.exe"), reverse=True)
    return [str(candidates[0])] if candidates else None


def _tool_major(command: list[str]) -> int:
    result = subprocess.run([*command, "--version"], capture_output=True, check=True, timeout=15)
    output = result.stdout.decode("utf-8", errors="replace")
    match = re.search(r"PostgreSQL\)\s+(\d+)", output)
    if not match:
        raise ValueError(f"cannot parse PostgreSQL tool version from {command[-1]}")
    return int(match.group(1))


def _postgres_tool_env(command: list[str], password: str) -> dict[str, str]:
    environment = {**os.environ, "PGPASSWORD": password}
    if Path(command[0]).name.lower() not in {"wsl", "wsl.exe"}:
        return environment
    entries = [entry for entry in environment.get("WSLENV", "").split(":") if entry]
    names = {entry.split("/", 1)[0].upper() for entry in entries}
    if "PGPASSWORD" not in names:
        entries.append("PGPASSWORD")
    environment["WSLENV"] = ":".join(entries)
    return environment


def _run_postgres_tool(command: list[str], password: str, **kwargs) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, env=_postgres_tool_env(command, password), check=True, timeout=120, **kwargs)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"{command[-1]} timed out after 120 seconds") from None
    except subprocess.CalledProcessError as error:
        summary = (error.stderr or b"").decode("utf-8", errors="replace").replace(password, "[redacted]").strip()
        if len(summary) > 500:
            summary = f"{summary[:500]}..."
        detail = f": {summary}" if summary else ""
        raise AssertionError(f"{command[-1]} failed with exit code {error.returncode}{detail}") from None


def _credential_free_url_and_password(url: str) -> tuple[str, str]:
    parsed = make_url(url)
    username = quote(parsed.username or "", safe="")
    host = parsed.host or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{username}@{host}" if username else host
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    database = f"/{quote(unquote(parsed.database), safe='')}" if parsed.database else ""
    query = f"?{urlencode(parsed.query, doseq=True)}" if parsed.query else ""
    cli_url = f"postgresql://{authority}{database}{query}"
    return cli_url, parsed.password or ""


def test_postgres_identifier_quoting_escapes_role_names() -> None:
    assert _quote_identifier('admin"name') == '"admin""name"'
    with pytest.raises(ValueError):
        _quote_identifier("admin\x00name")


def test_postgres_tool_prefix_is_parsed_as_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX", '["wsl","-d","Ubuntu-24.04","--"]')

    assert _postgres_tool("pg_dump") == ["wsl", "-d", "Ubuntu-24.04", "--", "pg_dump"]


@pytest.mark.parametrize("prefix", ['"wsl"', "[]", '["wsl",""]', "not-json"])
def test_postgres_tool_prefix_rejects_invalid_json_argv(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv("COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX", prefix)

    with pytest.raises(ValueError, match="JSON string array"):
        _postgres_tool("pg_dump")


def test_postgres_cli_url_removes_async_driver_and_password() -> None:
    cli_url, password = _credential_free_url_and_password(
        "postgresql+asyncpg://test%20user:p%40ss%3Aword@127.0.0.1:5432/control%20plane?sslmode=disable"
    )

    assert cli_url == "postgresql://test%20user@127.0.0.1:5432/control%20plane?sslmode=disable"
    assert password == "p@ss:word"
    assert password not in cli_url
    assert "asyncpg" not in cli_url


def test_postgres_tool_version_decodes_non_locale_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=b"pg_dump (PostgreSQL) 16.14\xff\xfe", stderr=b"\x81")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _tool_major(["wsl", "--", "pg_dump"]) == 16


@pytest.mark.parametrize("existing", [None, "FOO/u:BAR/p"])
def test_postgres_tool_env_adds_password_to_wslenv(monkeypatch: pytest.MonkeyPatch, existing: str | None) -> None:
    if existing is None:
        monkeypatch.delenv("WSLENV", raising=False)
    else:
        monkeypatch.setenv("WSLENV", existing)

    environment = _postgres_tool_env(["wsl.exe", "--", "pg_dump"], "secret")

    assert environment["PGPASSWORD"] == "secret"
    assert environment["WSLENV"] == ("PGPASSWORD" if existing is None else f"{existing}:PGPASSWORD")


def test_postgres_tool_env_preserves_existing_password_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSLENV", "FOO:PGPASSWORD/u:BAR")

    environment = _postgres_tool_env(["wsl", "--", "pg_restore"], "secret")

    assert environment["WSLENV"] == "FOO:PGPASSWORD/u:BAR"


def test_postgres_tool_env_does_not_change_wslenv_for_native_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSLENV", "FOO/u")

    environment = _postgres_tool_env([r"C:\PostgreSQL\bin\pg_dump.exe"], "secret")

    assert environment["WSLENV"] == "FOO/u"
    assert environment["PGPASSWORD"] == "secret"


def test_postgres_tool_error_redacts_password(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(2, ["wsl", "--", "pg_dump"], stderr=b"password secret was rejected")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AssertionError, match=r"pg_dump failed with exit code 2: password \[redacted\] was rejected"):
        _run_postgres_tool(["wsl", "--", "pg_dump"], "secret", capture_output=True)


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


@pytest.mark.integration
def test_postgres_custom_backup_restore_preserves_pending_control_plane_state(
    migrated_postgres_url: str,
) -> None:
    pg_dump, pg_restore = _postgres_tool("pg_dump"), _postgres_tool("pg_restore")
    if not pg_dump or not pg_restore:
        pytest.skip(
            "pg_dump and pg_restore are required; add PostgreSQL bin to PATH or set "
            "COMPANY_AGENT_TEST_PG_DUMP and COMPANY_AGENT_TEST_PG_RESTORE"
        )
    admin_url = os.environ[ADMIN_URL_ENV]

    async def server_major() -> int:
        import asyncpg

        connection = await asyncpg.connect(_asyncpg_url(admin_url))
        try:
            return int(await connection.fetchval("SHOW server_version_num")) // 10000
        finally:
            await connection.close()

    major = asyncio.run(server_major())
    if _tool_major(pg_dump) != major or _tool_major(pg_restore) != major:
        pytest.skip(f"pg_dump and pg_restore must match PostgreSQL server major version {major}")

    async def seed_source():
        engine = create_async_engine(migrated_postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                team = Team(slug=f"backup-{uuid.uuid4().hex}", name="Backup")
                session.add(team)
                await session.flush()
                user = User(team_id=team.id, email="backup@example.test", password_hash="hash")
                session.add(user)
                await session.flush()
                team_id, email = team.id, user.email
                await session.commit()
                store = PostgresStore(session)
                identity = await store.find_user(team_id, email)
                await store.create_pairing_code(identity, "backup-pair", utcnow() + timedelta(minutes=5))
                device, credential, projects = await store.consume_pairing_code(
                    "backup-pair", "backup-device", "backup-key", "runtime", ["Backup project"]
                )
                conversation = await store.create_conversation(identity, "Backup conversation")
                task, _ = await store.create_task(
                    identity, device.id, projects[0].id, conversation.id, "backup-task", "prompt"
                )
                await store.acknowledge_delivery(device, task.id, task.delivery_id)
                await store.append_event(device, task.id, "backup-event", "text.delta", {"text": "persisted"})
                await store.append_event(
                    device,
                    task.id,
                    "backup-approval",
                    "item/commandExecution/requestApproval",
                    {"request_id": 91},
                )
                approval = (await store.list_approvals(task.id, identity))[0]
                decided, _ = await store.decide_approval(approval.id, identity, "approved")
                token = "backup-model-token"
                authorization = await store.create_model_token(
                    device,
                    task.id,
                    token,
                    f"backup-jti-{uuid.uuid4().hex}",
                    "deepseek-v4-flash",
                    utcnow() + timedelta(minutes=5),
                )
                await store.record_model_usage(authorization, "backup-provider-request", 4, 6)
                return identity, device, credential, task, approval.id, decided.decision_delivery_id, team_id
        finally:
            await engine.dispose()

    identity, device, credential, task, approval_id, approval_delivery_id, team_id = asyncio.run(seed_source())
    suffix = uuid.uuid4().hex
    role, database, password = f"company_agent_restore_{suffix}", f"company_agent_restore_{suffix}", uuid.uuid4().hex
    parsed_admin = make_url(admin_url)
    qr, qd, qa = _quote_identifier(role), _quote_identifier(database), _quote_identifier(parsed_admin.username)
    restored_url = parsed_admin.set(username=role, password=password, database=database).render_as_string(
        hide_password=False
    )
    role_created = membership_granted = database_created = False
    try:
        asyncio.run(_admin_execute(admin_url, f"CREATE ROLE {qr} LOGIN PASSWORD {_quote_literal(password)}"))
        role_created = True
        asyncio.run(_admin_execute(admin_url, f"GRANT {qr} TO {qa}"))
        membership_granted = True
        asyncio.run(_admin_execute(admin_url, f"CREATE DATABASE {qd} OWNER {qr}"))
        database_created = True
        source_cli_url, source_password = _credential_free_url_and_password(migrated_postgres_url)
        restored_cli_url, restored_password = _credential_free_url_and_password(restored_url)
        with tempfile.TemporaryDirectory(prefix="company-agent-pg-backup-") as temporary:
            dump_path = Path(temporary) / "control-plane.dump"
            with dump_path.open("wb") as dump_file:
                _run_postgres_tool(
                    [*pg_dump, "--format=custom", "--no-owner", "--dbname", source_cli_url],
                    source_password,
                    stdout=dump_file,
                    stderr=subprocess.PIPE,
                )
            assert dump_path.stat().st_size > 0
            with dump_path.open("rb") as dump_file:
                _run_postgres_tool(
                    [*pg_restore, "--no-owner", "--exit-on-error", "--dbname", restored_cli_url],
                    restored_password,
                    stdin=dump_file,
                    capture_output=True,
                )

        restored_schema = asyncio.run(_inspect_database(restored_url))
        assert len(restored_schema["tables"] - {"alembic_version"}) == 18
        assert {"tasks", "task_events", "approval_requests", "audit_events", "model_usage"} <= restored_schema["tables"]
        assert restored_schema["foreign_key_count"] == 21
        assert restored_schema["unique_constraint_count"] == 26
        assert restored_schema["task_status_values"] == EXPECTED_TASK_STATUSES

        async def verify_restore() -> None:
            engine = create_async_engine(restored_url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with sessions() as session:
                    store = PostgresStore(session)
                    assert await store.authenticate_device(device.id, credential) == device
                    pending_tasks = await store.pending_tasks_for_device(device)
                    assert [item.id for item in pending_tasks] == [task.id]
                    assert await store.acknowledge_delivery(device, task.id, task.delivery_id)
                    pending_approvals = await store.pending_approval_decisions(device)
                    assert len(pending_approvals) == 1
                    assert pending_approvals[0].id == approval_id
                    assert pending_approvals[0].decision_delivery_id == approval_delivery_id
                    events = await store.events_after(task, 0)
                    assert [(item.sequence, item.source_event_id) for item in events] == [
                        (1, "backup-event"),
                        (2, "backup-approval"),
                    ]
                    assert await store.model_usage_total(team_id) == 10
                    audit = {item.event_type for item in await store.task_audit(task.id, identity)}
                    assert {
                        "task.created",
                        "task.delivery_acknowledged",
                        "approval.approved",
                        "model.token_issued",
                    } <= audit
                    assert await store.acknowledge_approval_decision(device, approval_id, approval_delivery_id)
                async with sessions() as session:
                    assert await PostgresStore(session).pending_approval_decisions(device) == []
            finally:
                await engine.dispose()

        asyncio.run(verify_restore())
    finally:
        cleanup_failures: list[Exception] = []
        if database_created:
            try:
                asyncio.run(
                    _admin_execute(
                        admin_url,
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        database,
                    )
                )
            except PostgresError as error:
                cleanup_failures.append(error)
            try:
                asyncio.run(_admin_execute(admin_url, f"DROP DATABASE {qd}"))
            except PostgresError as error:
                cleanup_failures.append(error)
        if membership_granted:
            try:
                asyncio.run(_admin_execute(admin_url, f"REVOKE {qr} FROM {qa}"))
            except PostgresError as error:
                cleanup_failures.append(error)
        if role_created:
            try:
                asyncio.run(_admin_execute(admin_url, f"DROP ROLE {qr}"))
            except PostgresError as error:
                cleanup_failures.append(error)
        if cleanup_failures and sys.exc_info()[0] is None:
            raise cleanup_failures[0]


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
