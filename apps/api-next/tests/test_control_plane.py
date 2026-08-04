import os
import uuid
import asyncio
from datetime import timedelta

os.environ.setdefault("COMPANY_AGENT_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.dependencies import get_store
from app.main import app, websocket_store
from app.security import hash_password, hash_secret, utcnow
from app.store import DeviceIdentity, MemoryStore, UserIdentity
from app.sse import replay_and_follow


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
    store.projects[project_a], store.projects[project_b] = (team_a, device_a.id, "root-a"), (team_b, device_b.id, "root-b")
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
    token = "browser-session"
    store.sessions[hash_secret(token)] = (user, utcnow() + timedelta(hours=1), None)
    return {"Cookie": f"company_session={token}"}


def test_login_and_expired_session_are_checked(client, seeded_store):
    store, user, *_ = seeded_store
    response = client.post("/auth/login", json={"team_id": str(user.team_id), "email": user.email, "password": "correct horse"})
    assert response.status_code == 204
    assert "company_session=" in response.headers["set-cookie"]
    token = "expired-session"
    store.sessions[hash_secret(token)] = (user, utcnow() - timedelta(seconds=1), None)
    assert client.get("/auth/me", headers={"Cookie": f"company_session={token}"}).status_code == 401
    active = session_headers(store, user)
    assert client.post("/auth/logout", headers=active).status_code == 204
    assert client.get("/auth/me", headers=active).status_code == 401


def test_cross_team_task_is_rejected_and_requests_are_idempotent(client, seeded_store):
    store, user, _, device_a, device_b, project_a, project_b, conversation_a, conversation_b = seeded_store
    headers = session_headers(store, user)
    cross_team = client.post("/tasks", headers=headers, json={"device_id": str(device_b.id), "project_id": str(project_b), "conversation_id": str(conversation_b), "idempotency_key": "same", "prompt": "cross-team"})
    assert cross_team.status_code == 403
    body = {"device_id": str(device_a.id), "project_id": str(project_a), "conversation_id": str(conversation_a), "idempotency_key": "same", "prompt": "implement the task"}
    first, second = client.post("/tasks", headers=headers, json=body), client.post("/tasks", headers=headers, json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_device_websocket_auth_event_deduplication_and_sse_replay(client, seeded_store):
    store, user, _, device_a, _, project_a, _, conversation_a, _ = seeded_store
    headers = session_headers(store, user)
    task = client.post("/tasks", headers=headers, json={"device_id": str(device_a.id), "project_id": str(project_a), "conversation_id": str(conversation_a), "idempotency_key": "stream", "prompt": "stream this task"}).json()
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device_a.id), "credential": "device-a-secret", "runtime_version": "1.0"})
        assert socket.receive_json()["type"] == "authenticated"
        dispatch = socket.receive_json()
        assert dispatch["type"] == "task.dispatch"
        socket.send_json({"type": "task.dispatch.ack", "task_id": task["id"], "delivery_id": dispatch["delivery_id"]})
        assert socket.receive_json()["accepted"] is True
        message = {"type": "task.event", "task_id": task["id"], "source_event_id": "event-later", "event_type": "command.completed", "payload": {"exit_code": 0}}
        socket.send_json(message)
        assert socket.receive_json()["sequence"] == 1
        socket.send_json({**message, "source_event_id": "event-earlier", "event_type": "text.delta", "payload": {"text": "done"}})
        assert socket.receive_json()["sequence"] == 2
        socket.send_json(message)
        assert socket.receive_json()["sequence"] == 1
    replay = client.get(f"/tasks/{task['id']}/events?follow=false", headers={**headers, "Last-Event-ID": "1"})
    assert replay.status_code == 200
    assert "id: 2" in replay.text and "id: 1" not in replay.text
    assert "text.delta" in replay.text


def test_device_websocket_rejects_invalid_credential(client, seeded_store):
    _, _, _, device_a, *_ = seeded_store
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/devices") as socket:
            socket.send_json({"type": "authenticate", "device_id": str(device_a.id), "credential": "not-the-credential"})
            socket.receive_json()
    assert error.value.code == 1008


def test_connected_device_receives_new_task_dispatch(client, seeded_store):
    store, user, _, device_a, _, project_a, _, conversation_a, _ = seeded_store
    headers = session_headers(store, user)
    with client.websocket_connect("/ws/devices") as socket:
        socket.send_json({"type": "authenticate", "device_id": str(device_a.id), "credential": "device-a-secret"})
        assert socket.receive_json()["type"] == "authenticated"
        response = client.post("/tasks", headers=headers, json={"device_id": str(device_a.id), "project_id": str(project_a), "conversation_id": str(conversation_a), "idempotency_key": "live-dispatch", "prompt": "live dispatch prompt"})
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
