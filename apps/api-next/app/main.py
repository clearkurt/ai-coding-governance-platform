from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal, engine, get_session
from app.device_registry import DeviceConnectionRegistry
from app.dependencies import SESSION_COOKIE, current_user, get_store, new_session_token, session_expiry
from app.schemas import CreateTaskRequest, CurrentUser, DeviceAuthentication, DeviceEventMessage, LoginRequest, TaskOut
from app.security import verify_password
from app.store import DeviceIdentity, PostgresStore, Store, UserIdentity
from app.sse import replay_and_follow


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Company Agent API", version="0.1.0", lifespan=lifespan)
app.state.device_registry = DeviceConnectionRegistry()


def task_out(task) -> TaskOut:
    return TaskOut(id=task.id, team_id=task.team_id, device_id=task.device_id, project_id=task.project_id, conversation_id=task.conversation_id, root_id=task.root_id, status=task.status)


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable") from error
    return {"status": "ok"}


@app.post("/auth/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(body: LoginRequest, response: Response, store: Store = Depends(get_store)) -> Response:
    user = await store.find_user(body.team_id, body.email)
    if not user or not user.is_active or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = new_session_token()
    expires_at = session_expiry()
    await store.create_session(user, token, expires_at)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="lax", expires=expires_at, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response, company_session: str | None = Cookie(default=None), store: Store = Depends(get_store)
) -> Response:
    # Logout remains idempotent but revokes the server-side token if one was presented.
    if company_session:
        await store.revoke_session(company_session)
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax", path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.get("/auth/me", response_model=CurrentUser)
async def me(user: UserIdentity = Depends(current_user)) -> CurrentUser:
    return CurrentUser(id=user.id, team_id=user.team_id, email=user.email)


@app.post("/tasks", response_model=TaskOut)
async def create_task(body: CreateTaskRequest, response: Response, user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)) -> TaskOut:
    try:
        task, created = await store.create_task(user, body.device_id, body.project_id, body.conversation_id, body.idempotency_key, body.prompt)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="project, device, or conversation is not available") from error
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    if created:
        await app.state.device_registry.deliver(task)
    return task_out(task)


@app.get("/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    follow: bool = True,
    user: UserIdentity = Depends(current_user),
    store: Store = Depends(get_store),
) -> StreamingResponse:
    header_value = request.headers.get("last-event-id") or last_event_id or "0"
    try:
        after_sequence = max(0, int(header_value))
        import uuid

        parsed_task_id = uuid.UUID(task_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid task or event sequence") from error
    task = await store.get_task_for_user(parsed_task_id, user)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    return StreamingResponse(replay_and_follow(store, task, after_sequence, follow=follow), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def websocket_store() -> Store:
    # WebSocket dependencies cannot keep a request-scoped session open across a device lifetime.
    # Each connection owns one PostgreSQL session and is closed in the endpoint finally block.
    return PostgresStore(SessionLocal())


@app.websocket("/ws/devices")
async def device_gateway(websocket: WebSocket, store: Store = Depends(websocket_store)) -> None:
    await websocket.accept()
    device: DeviceIdentity | None = None
    try:
        raw_auth = await websocket.receive_json()
        auth = DeviceAuthentication.model_validate(raw_auth)
        if auth.type != "authenticate":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        device = await store.authenticate_device(auth.device_id, auth.credential)
        if not device:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await store.touch_device(device, auth.runtime_version)
        await app.state.device_registry.register(device.id, websocket)
        await websocket.send_json({"type": "authenticated", "device_id": str(device.id)})
        for pending_task in await store.pending_tasks_for_device(device):
            await app.state.device_registry.deliver(pending_task)
        while True:
            raw = await websocket.receive_json()
            if raw.get("type") == "heartbeat":
                await store.touch_device(device, raw.get("runtime_version"))
                await websocket.send_json({"type": "heartbeat.ack"})
                continue
            if raw.get("type") == "task.dispatch.ack":
                try:
                    import uuid

                    task_id = uuid.UUID(raw["task_id"])
                    delivery_id = str(raw["delivery_id"])
                except (KeyError, ValueError):
                    await websocket.send_json({"type": "error", "code": "invalid_delivery_ack"})
                    continue
                accepted = await store.acknowledge_delivery(device, task_id, delivery_id)
                await websocket.send_json({"type": "task.dispatch.acknowledged", "task_id": str(task_id), "accepted": accepted})
                continue
            message = DeviceEventMessage.model_validate(raw)
            if message.type != "task.event":
                await websocket.send_json({"type": "error", "code": "unsupported_message"})
                continue
            try:
                event = await store.append_event(device, message.task_id, message.source_event_id, message.event_type, message.payload)
            except PermissionError:
                await websocket.send_json({"type": "error", "code": "task_not_assigned"})
                continue
            await websocket.send_json({"type": "task.event.ack", "task_id": str(event.task_id), "source_event_id": event.source_event_id, "sequence": event.sequence})
    except (WebSocketDisconnect, ValidationError):
        if not websocket.client_state.name.lower() == "disconnected":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    finally:
        if device:
            await app.state.device_registry.unregister(device.id, websocket)
        if isinstance(store, PostgresStore):
            await store.session.close()
