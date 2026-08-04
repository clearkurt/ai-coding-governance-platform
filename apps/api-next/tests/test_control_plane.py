import asyncio
import os
import uuid
from datetime import timedelta

os.environ.setdefault("COMPANY_AGENT_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("COMPANY_AGENT_DEEPSEEK_API_KEY", "server-only-deepseek-secret")
os.environ.setdefault("COMPANY_AGENT_DEEPSEEK_BASE_URL", "https://deepseek.test")

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.dependencies import get_store
from app.main import app, websocket_store
from app.model_proxy import get_model_http_client
from app.security import hash_password, hash_secret, utcnow
from app.sse import replay_and_follow
from app.store import DeviceIdentity, MemoryStore, UserIdentity


@pytest.fixture
def seeded_store():
    store = MemoryStore()
    team_a, team_b = uuid.uuid4(), uuid.uuid4()
    user_a = UserIdentity(uuid.uuid4(), team_a, "member@example.com", hash_password("correct horse"))
    user_b = UserIdentity(uuid.uuid4(), team_b, "other@example.com", hash_password("other password"))
    device_a, device_b = DeviceIdentity(uuid.uuid4(), team_a), DeviceIdentity(uuid.uuid4(), team_b)
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    conversation_a, conversation_b = uuid.uuid4(), uuid.uuid4()
    store.users[(team_a, user_a.email)] = user_a
    store.users[(team_b, user_b.email)] = user_b
    store.devices[device_a.id], store.devices[device_b.id] = device_a, device_b
    store.credentials[hash_secret("device-a-secret")] = (device_a.id, utcnow() + timedelta(hours=1), None)
    store.credentials[hash_secret("device-b-secret")] = (device_b.id, utcnow() + timedelta(hours=1), None)
    store.projects[project_a], store.projects[project_b] = (
        (team_a, device_a.id, "root-a"),
        (team_b, device_b.id, "root-b"),
    )
    store.conversations[conversation_a], store.conversations[conversation_b] = team_a, team_b
    store.grants.add((user_a.id, project_a))
    return store, user_a, user_b, device_a, device_b, project_a, project_b, conversation_a, conversation_b


@pytest.fixture
def client(seeded_store):
    store = seeded_store[0]

    async def override_store():
        yield store

    async def override_websocket_store():
        return store

    app.dependency_overrides[get_store] = override_store
    app.dependency_overrides[websocket_store] = override_websocket_store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def session_headers(store, user):
    token = f"browser-session-{user.id}"
    store.sessions[hash_secret(token)] = (user, utcnow() + timedelta(hours=1), None)
    return {"Cookie": f"company_session={token}"}


def test_login_and_expired_session_are_checked(client, seeded_store):
    store, user, *_ = seeded_store
    response = client.post(
        "/auth/login", json={"team_id": str(user.team_id), "email": user.email, "password": "correct horse"}
    )
    assert response.status_code == 204
    assert "company_session=" in response.headers["set-cookie"]
    token = "expired-session"
    store.sessions[hash_secret(token)] = (user, utcnow() - timedelta(seconds=1), None)
    assert client.get("/auth/me", headers={"Cookie": f"company_session={token}"}).status_code == 401
    active = session_headers(store, user)
    assert client.post("/auth/logout", headers=active).status_code == 204
    assert client.get("/auth/me", headers=active).status_code == 401


def test_pairing_code_is_single_use_and_creates_granted_resources(client, seeded_store):
    store, user, *_ = seeded_store
    headers = session_headers(store, user)
    issued = client.post("/pairing-codes", headers=headers)
    assert issued.status_code == 200
    code = issued.json()["code"]
    assert code not in store.pairing_codes
    pair = {
        "version": 1,
        "messageId": "pair-message",
        "type": "pair",
        "payload": {
            "code": code,
            "name": "Workshop PC",
            "publicKey": "public-key",
            "version": "0.1.0",
            "roots": [{"label": "Firmware"}],
        },
    }
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json(pair)
        result = socket.receive_json()
    assert result["type"] == "pair_result"
    assert result["payload"]["credential"] not in store.credentials
    assert len(result["payload"]["roots"]) == 1
    projects = client.get("/projects", headers=headers).json()
    assert any(project["display_name"] == "Firmware" for project in projects)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/devices") as socket:
        socket.send_json(pair)
        assert socket.receive_json()["type"] == "pair_error"
        socket.receive_json()
    assert all(
        code not in str(event) and result["payload"]["credential"] not in str(event) for event in store.control_audit
    )


def test_expired_pairing_code_and_concurrent_consumption_are_rejected(seeded_store):
    store, user, *_ = seeded_store
    expired = "expired-code"
    store.pairing_codes[hash_secret(expired)] = (user, utcnow() - timedelta(seconds=1), None)

    async def exercise():
        with pytest.raises(PermissionError):
            await store.consume_pairing_code(expired, "PC", "key", "1", ["Root"])
        raw = "one-winner"
        await store.create_pairing_code(user, raw, utcnow() + timedelta(minutes=10))
        results = await asyncio.gather(
            store.consume_pairing_code(raw, "A", "key-a", "1", ["Root"]),
            store.consume_pairing_code(raw, "B", "key-b", "1", ["Root"]),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, PermissionError) for result in results) == 1

    asyncio.run(exercise())


def test_resource_discovery_and_conversations_are_user_and_team_scoped(client, seeded_store):
    store, user_a, user_b, device_a, _, project_a, _, *_ = seeded_store
    headers_a, headers_b = session_headers(store, user_a), session_headers(store, user_b)
    store.project_names[project_a] = "Allowed project"
    store.device_metadata[device_a.id] = ("Device A", "codex-1", utcnow())
    created = client.post("/conversations", headers=headers_a, json={"title": "Private conversation"})
    assert created.status_code == 201
    assert client.get("/conversations", headers=headers_a).json()[0]["title"] == "Private conversation"
    assert client.get("/conversations", headers=headers_b).json() == []
    devices = client.get("/devices", headers=headers_a).json()
    assert devices[0]["name"] == "Device A" and devices[0]["online"] is False
    assert devices[0]["projects"][0]["display_name"] == "Allowed project"
    assert all(
        project["device_id"] == str(device_a.id) for project in client.get("/projects", headers=headers_a).json()
    )
    assert client.get("/devices", headers=headers_b).json() == []
    store.devices[device_a.id] = DeviceIdentity(device_a.id, device_a.team_id, utcnow())
    assert client.get("/devices", headers=headers_a).json() == []


def test_cross_team_task_is_rejected_and_requests_are_idempotent(client, seeded_store):
    store, user, _, device_a, device_b, project_a, project_b, conversation_a, conversation_b = seeded_store
    headers = session_headers(store, user)
    cross_team = client.post(
        "/tasks",
        headers=headers,
        json={
            "device_id": str(device_b.id),
            "project_id": str(project_b),
            "conversation_id": str(conversation_b),
            "idempotency_key": "same",
            "prompt": "cross-team",
        },
    )
    assert cross_team.status_code == 403
    body = {
        "device_id": str(device_a.id),
        "project_id": str(project_a),
        "conversation_id": str(conversation_a),
        "idempotency_key": "same",
        "prompt": "implement the task",
    }
    first, second = client.post("/tasks", headers=headers, json=body), client.post("/tasks", headers=headers, json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    competing = client.post("/tasks", headers=headers, json={**body, "idempotency_key": "competing"})
    assert competing.status_code == 409


def test_device_websocket_auth_event_deduplication_and_sse_replay(client, seeded_store):
    store, user, _, device_a, _, project_a, _, conversation_a, _ = seeded_store
    headers = session_headers(store, user)
    task = client.post(
        "/tasks",
        headers=headers,
        json={
            "device_id": str(device_a.id),
            "project_id": str(project_a),
            "conversation_id": str(conversation_a),
            "idempotency_key": "stream",
            "prompt": "stream this task",
        },
    ).json()
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json(
            {
                "type": "authenticate",
                "device_id": str(device_a.id),
                "credential": "device-a-secret",
                "runtime_version": "1.0",
            }
        )
        assert socket.receive_json()["type"] == "authenticated"
        dispatch = socket.receive_json()
        assert dispatch["type"] == "task.dispatch"
        socket.send_json({"type": "task.dispatch.ack", "task_id": task["id"], "delivery_id": dispatch["delivery_id"]})
        assert socket.receive_json()["accepted"] is True
        message = {
            "type": "task.event",
            "task_id": task["id"],
            "source_event_id": "event-later",
            "event_type": "command.completed",
            "payload": {"exit_code": 0},
        }
        socket.send_json(message)
        assert socket.receive_json()["sequence"] == 1
        socket.send_json(
            {**message, "source_event_id": "event-earlier", "event_type": "text.delta", "payload": {"text": "done"}}
        )
        assert socket.receive_json()["sequence"] == 2
        socket.send_json(message)
        assert socket.receive_json()["sequence"] == 1
    replay = client.get(f"/tasks/{task['id']}/events?follow=false", headers={**headers, "Last-Event-ID": "1"})
    assert replay.status_code == 200
    assert "id: 2" in replay.text and "id: 1" not in replay.text
    assert "text.delta" in replay.text


def test_device_websocket_rejects_invalid_credential(client, seeded_store):
    _, _, _, device_a, *_ = seeded_store
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device_a.id), "credential": "not-the-credential"})
        socket.receive_json()
    assert error.value.code == 1008


def test_connected_device_receives_new_task_dispatch(client, seeded_store):
    store, user, _, device_a, _, project_a, _, conversation_a, _ = seeded_store
    headers = session_headers(store, user)
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device_a.id), "credential": "device-a-secret"})
        assert socket.receive_json()["type"] == "authenticated"
        response = client.post(
            "/tasks",
            headers=headers,
            json={
                "device_id": str(device_a.id),
                "project_id": str(project_a),
                "conversation_id": str(conversation_a),
                "idempotency_key": "live-dispatch",
                "prompt": "live dispatch prompt",
            },
        )
        assert response.status_code == 201
        dispatch = socket.receive_json()
        assert dispatch["type"] == "task.dispatch" and dispatch["task_id"] == response.json()["id"]
        assert dispatch["root_id"] == "root-a" and dispatch["prompt"] == "live dispatch prompt"


@pytest.mark.asyncio
async def test_sse_follows_event_that_arrives_after_subscription(seeded_store):
    store, user, _, device_a, _, project_a, _, conversation_a, _ = seeded_store
    task, _ = await store.create_task(user, device_a.id, project_a, conversation_a, "follow", "follow prompt")
    stream = replay_and_follow(store, task, 0, poll_interval=0.01)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.02)
    await store.append_event(device_a, task.id, "after-subscribe", "text.delta", {"text": "later"})
    assert "id: 1" in await asyncio.wait_for(pending, timeout=1)
    await stream.aclose()


def create_task_and_model_token(client, seeded_store, key="model"):
    store, user, _, device, _, project, _, conversation, _ = seeded_store
    headers = session_headers(store, user)
    task = client.post(
        "/tasks",
        headers=headers,
        json={
            "device_id": str(device.id),
            "project_id": str(project),
            "conversation_id": str(conversation),
            "idempotency_key": key,
            "prompt": "fix",
        },
    ).json()
    token = client.post(
        "/model-tokens",
        headers={"X-Device-ID": str(device.id), "Authorization": "Bearer device-a-secret"},
        json={"task_id": task["id"], "model": "deepseek-v4-flash"},
    )
    assert token.status_code == 200
    return task, token.json()["access_token"], headers


def test_responses_sse_is_transparent_key_is_hidden_and_usage_is_idempotent(client, seeded_store):
    store, *_ = seeded_store
    _, token, _ = create_task_and_model_token(client, seeded_store)
    seen = []
    payload = b'data: {"type":"response.completed","response":{"id":"resp-same","usage":{"input_tokens":7,"output_tokens":3}}}'

    class OneChunk(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload

    async def handler(request: httpx.Request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer server-only-deepseek-secret"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-request-id": "resp-same"}, stream=OneChunk()
        )

    transport = httpx.MockTransport(handler)

    async def override_client():
        async with httpx.AsyncClient(transport=transport, base_url="https://deepseek.test") as upstream:
            yield upstream

    app.dependency_overrides[get_model_http_client] = override_client
    try:
        for _ in range(2):
            response = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "deepseek-v4-flash", "stream": True, "input": "hello"},
            )
            assert response.status_code == 200 and response.content == payload
            assert b"server-only-deepseek-secret" not in response.content
        assert store.usage_requests == {"resp-same"}
        invalid = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "deepseek-v4-flash", "input": "x"},
        )
        assert invalid.status_code == 401
        wrong_model = client.post(
            "/v1/responses", headers={"Authorization": f"Bearer {token}"}, json={"model": "other", "input": "x"}
        )
        assert wrong_model.status_code == 400
        json_array = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            content="[]",
        )
        assert json_array.status_code == 400
    finally:
        app.dependency_overrides.pop(get_model_http_client, None)


def test_model_token_is_task_and_device_bound(client, seeded_store):
    store, user, _, device_a, device_b, project_a, _, conversation_a, _ = seeded_store
    headers = session_headers(store, user)
    task = client.post(
        "/tasks",
        headers=headers,
        json={
            "device_id": str(device_a.id),
            "project_id": str(project_a),
            "conversation_id": str(conversation_a),
            "idempotency_key": "bound",
            "prompt": "fix",
        },
    ).json()
    cross = client.post(
        "/model-tokens",
        headers={"X-Device-ID": str(device_b.id), "Authorization": "Bearer device-b-secret"},
        json={"task_id": task["id"], "model": "deepseek-v4-flash"},
    )
    assert cross.status_code == 403
    denied = client.post(
        "/model-tokens",
        headers={"X-Device-ID": str(device_a.id), "Authorization": "Bearer device-a-secret"},
        json={"task_id": task["id"], "model": "not-allowed"},
    )
    assert denied.status_code == 400


def test_codex_model_catalog_is_task_token_authenticated_and_fixed(client, seeded_store):
    _, token, _ = create_task_and_model_token(client, seeded_store, "catalog")
    denied = client.get("/v1/models")
    assert denied.status_code == 401
    response = client.get("/v1/models?client_version=0.145.0", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["x-model-catalog-version"] == "deepseek-v4-flash-1"
    assert [model["slug"] for model in response.json()["models"]] == ["deepseek-v4-flash"]


def test_approval_decision_is_idempotent_cross_team_safe_and_cancel_routes(client, seeded_store):
    store, _, user_b, device, _, _, _, _, _ = seeded_store
    task, _, headers = create_task_and_model_token(client, seeded_store, "approval")
    event = asyncio.run(
        store.append_event(
            device, uuid.UUID(task["id"]), "approval-event", "item/commandExecution/requestApproval", {"request_id": 41}
        )
    )
    assert "approval_id" in event.payload
    items = client.get(f"/tasks/{task['id']}/approvals", headers=headers).json()
    approval_id = items[0]["id"]

    class CapturingRegistry:
        def __init__(self):
            self.messages = []

        async def send(self, device_id, payload):
            self.messages.append((device_id, payload))
            return True

    registry = CapturingRegistry()
    original_registry = app.state.device_registry
    app.state.device_registry = registry
    other_headers = session_headers(store, user_b)
    assert (
        client.post(
            f"/approvals/{approval_id}/decision", headers=other_headers, json={"decision": "approved"}
        ).status_code
        == 404
    )
    try:
        first = client.post(f"/approvals/{approval_id}/decision", headers=headers, json={"decision": "approved"})
        second = client.post(f"/approvals/{approval_id}/decision", headers=headers, json={"decision": "approved"})
        assert first.json()["changed"] is True and second.json()["changed"] is False
        assert registry.messages[0][1]["request_id"] == 41 and registry.messages[0][1]["approved"] is True
        assert registry.messages[0][1]["result"]["decision"] == "accept"
        assert registry.messages[0][1]["delivery_id"] == registry.messages[1][1]["delivery_id"]
        cancelled = client.post(f"/tasks/{task['id']}/cancel", headers=headers)
        repeated = client.post(f"/tasks/{task['id']}/cancel", headers=headers)
        assert cancelled.json() == {"id": task["id"], "status": "cancelled", "changed": True}
        assert repeated.json()["changed"] is False
        assert registry.messages[-1][1]["type"] == "task.cancel"
    finally:
        app.state.device_registry = original_registry


def test_pending_approval_decision_replays_on_device_reconnect_and_acknowledges(client, seeded_store):
    store, _, _, device, _, _, _, _, _ = seeded_store
    task, _, headers = create_task_and_model_token(client, seeded_store, "approval-replay")
    asyncio.run(
        store.append_event(
            device,
            uuid.UUID(task["id"]),
            "approval-replay-event",
            "item/fileChange/requestApproval",
            {"request_id": 77},
        )
    )
    approval = client.get(f"/tasks/{task['id']}/approvals", headers=headers).json()[0]
    original_registry = app.state.device_registry

    class OfflineRegistry:
        async def send(self, device_id, payload):
            return False

    app.state.device_registry = OfflineRegistry()
    try:
        decided = client.post(f"/approvals/{approval['id']}/decision", headers=headers, json={"decision": "rejected"})
        assert decided.status_code == 200
    finally:
        app.state.device_registry = original_registry
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device.id), "credential": "device-a-secret"})
        assert socket.receive_json()["type"] == "authenticated"
        assert socket.receive_json()["type"] == "task.dispatch"
        replay = socket.receive_json()
        assert replay["type"] == "approval.decision"
        assert replay["request_id"] == 77 and replay["approved"] is False
        assert replay["result"]["decision"] == "decline"
        socket.send_json(
            {"type": "approval.decision.ack", "approval_id": approval["id"], "delivery_id": replay["delivery_id"]}
        )
        assert socket.receive_json()["accepted"] is True
    assert asyncio.run(store.pending_approval_decisions(device)) == []


def test_task_lifecycle_tracks_delivery_approval_and_terminal_event(client, seeded_store):
    store, _, _, device, _, _, _, _, _ = seeded_store
    task, token, _ = create_task_and_model_token(client, seeded_store, "lifecycle")
    task_id = uuid.UUID(task["id"])

    accepted = asyncio.run(store.acknowledge_delivery(device, task_id, str(task_id)))
    assert accepted is True
    assert store.tasks[task_id].status == "running"

    asyncio.run(
        store.append_event(
            device,
            task_id,
            "lifecycle-approval",
            "item/commandExecution/requestApproval",
            {"request_id": 88},
        )
    )
    assert store.tasks[task_id].status == "waiting_approval"

    asyncio.run(store.append_event(device, task_id, "lifecycle-complete", "turn/completed", {}))
    assert store.tasks[task_id].status == "completed"
    assert asyncio.run(store.validate_model_token(token, "deepseek-v4-flash")) is None


def test_rollback_requires_terminal_task_and_replays_until_device_ack(client, seeded_store):
    store, _, _, device, _, _, _, _, _ = seeded_store
    task, _, headers = create_task_and_model_token(client, seeded_store, "rollback")
    task_id = uuid.UUID(task["id"])

    assert client.post(f"/tasks/{task_id}/rollback", headers=headers).status_code == 409
    asyncio.run(store.append_event(device, task_id, "rollback-complete", "turn/completed", {}))

    class OfflineRegistry:
        async def send(self, device_id, payload):
            return False

    original_registry = app.state.device_registry
    app.state.device_registry = OfflineRegistry()
    try:
        requested = client.post(f"/tasks/{task_id}/rollback", headers=headers)
        repeated = client.post(f"/tasks/{task_id}/rollback", headers=headers)
        assert requested.status_code == 200
        assert requested.json()["created"] is True
        assert repeated.json()["created"] is False
        assert requested.json()["delivery_id"] == repeated.json()["delivery_id"]
    finally:
        app.state.device_registry = original_registry

    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device.id), "credential": "device-a-secret"})
        assert socket.receive_json()["type"] == "authenticated"
        rollback = socket.receive_json()
        assert rollback == {
            "type": "task.rollback",
            "task_id": str(task_id),
            "root_id": store.tasks[task_id].root_id,
            "delivery_id": requested.json()["delivery_id"],
        }
        socket.send_json(
            {
                "type": "task.rollback.ack",
                "task_id": str(task_id),
                "delivery_id": rollback["delivery_id"],
                "status": "succeeded",
            }
        )
        acknowledged = socket.receive_json()
        assert acknowledged["type"] == "task.rollback.acknowledged"
        assert acknowledged["accepted"] is True

    assert asyncio.run(store.pending_rollbacks(device)) == []
    final = client.post(f"/tasks/{task_id}/rollback", headers=headers).json()
    assert final["status"] == "succeeded"
    assert final["created"] is False
    audit = client.get(f"/tasks/{task_id}/audit", headers=headers).json()
    event_types = {event["event_type"] for event in audit}
    assert {"task.created", "codex.turn/completed", "task.rollback_requested", "task.rollback_succeeded"} <= event_types


@pytest.mark.asyncio
async def test_failed_rollback_can_be_requested_again_with_a_new_delivery(seeded_store):
    store, user, _, device, _, project, _, conversation, _ = seeded_store
    task, _ = await store.create_task(user, device.id, project, conversation, "rollback-retry", "fix")
    await store.append_event(device, task.id, "retry-complete", "turn/completed", {})
    first, created = await store.request_rollback(task.id, user)
    assert created is True
    assert await store.acknowledge_rollback(device, task.id, first.delivery_id, "failed") is True
    second, created = await store.request_rollback(task.id, user)
    assert created is True
    assert second.delivery_id != first.delivery_id
    assert second.status == "requested"
