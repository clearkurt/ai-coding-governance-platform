import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal, engine, get_session
from app.dependencies import SESSION_COOKIE, current_user, get_store, new_session_token, session_expiry
from app.device_registry import DeviceConnectionRegistry
from app.model_proxy import model_catalog, proxy_responses
from app.schemas import (
    ApprovalDecisionRequest,
    CreateConversationRequest,
    CreateTaskRequest,
    CurrentUser,
    DeviceAuthentication,
    DeviceEventMessage,
    LegacyPairPayload,
    LoginRequest,
    ModelTokenRequest,
    ModelTokenResponse,
    TaskOut,
)
from app.security import utcnow, verify_password
from app.settings import get_settings
from app.sse import replay_and_follow
from app.store import DeviceIdentity, PostgresStore, Store, UserIdentity


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Company Agent API", version="0.1.0", lifespan=lifespan)
app.state.device_registry = DeviceConnectionRegistry()
app.post("/v1/responses")(proxy_responses)
app.get("/v1/models")(model_catalog)


def task_out(task) -> TaskOut:
    return TaskOut(
        id=task.id,
        team_id=task.team_id,
        device_id=task.device_id,
        project_id=task.project_id,
        conversation_id=task.conversation_id,
        root_id=task.root_id,
        status=task.status,
    )


def approval_delivery_payload(approval) -> dict[str, object]:
    return {
        "type": "approval.decision",
        "approval_id": str(approval.id),
        "task_id": str(approval.task_id),
        "delivery_id": approval.decision_delivery_id,
        "request_id": int(approval.provider_item_id),
        "approved": approval.status == "approved",
        "result": {"decision": "accept" if approval.status == "approved" else "decline"},
    }


def rollback_delivery_payload(rollback) -> dict[str, object]:
    return {
        "type": "task.rollback",
        "task_id": str(rollback.task_id),
        "root_id": rollback.root_id,
        "delivery_id": rollback.delivery_id,
    }


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
    if (
        not user
        or not user.is_active
        or not user.password_hash
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = new_session_token()
    expires_at = session_expiry()
    await store.create_session(user, token, expires_at)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        expires=expires_at,
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response, company_session: str | None = Cookie(default=None), store: Store = Depends(get_store)
) -> Response:
    # Logout remains idempotent but revokes the server-side token if one was presented.
    if company_session:
        await store.revoke_session(company_session)
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.get("/auth/me", response_model=CurrentUser)
async def me(user: UserIdentity = Depends(current_user)) -> CurrentUser:
    return CurrentUser(id=user.id, team_id=user.team_id, email=user.email)


@app.post("/pairing-codes")
async def create_pairing_code(user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)):
    raw = secrets.token_urlsafe(18)
    expires_at = utcnow() + timedelta(minutes=10)
    await store.create_pairing_code(user, raw, expires_at)
    return {"code": raw, "expires_at": expires_at}


def project_payload(project) -> dict[str, object]:
    return {
        "id": project.id,
        "device_id": project.device_id,
        "root_id": project.root_id,
        "display_name": project.display_name,
    }


@app.get("/projects")
async def projects(user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)):
    return [project_payload(project) for project in await store.list_projects(user)]


@app.get("/devices")
async def devices(user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)):
    result = []
    for device in await store.list_devices(user):
        result.append(
            {
                "id": device.id,
                "name": device.name,
                "runtime_version": device.runtime_version,
                "last_seen_at": device.last_seen_at,
                "online": await app.state.device_registry.is_online(device.id),
                "projects": [project_payload(project) for project in device.projects],
            }
        )
    return result


@app.get("/conversations")
async def conversations(user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)):
    return [
        {"id": item.id, "title": item.title, "created_at": item.created_at}
        for item in await store.list_conversations(user)
    ]


@app.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest, user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)
):
    item = await store.create_conversation(user, body.title)
    return {"id": item.id, "title": item.title, "created_at": item.created_at}


async def device_from_headers(
    store: Store = Depends(get_store), x_device_id: str = Header(alias="X-Device-ID"), authorization: str = Header()
) -> DeviceIdentity:
    import uuid

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "device credential required")
    try:
        device_id = uuid.UUID(x_device_id)
    except ValueError as error:
        raise HTTPException(401, "invalid device identity") from error
    device = await store.authenticate_device(device_id, authorization[7:])
    if not device:
        raise HTTPException(401, "invalid, expired, or revoked device credential")
    return device


@app.post("/model-tokens", response_model=ModelTokenResponse)
async def issue_model_token(
    body: ModelTokenRequest, device: DeviceIdentity = Depends(device_from_headers), store: Store = Depends(get_store)
) -> ModelTokenResponse:
    settings = get_settings()
    if body.model != "deepseek-v4-flash":
        raise HTTPException(400, "model is not allowed")
    raw = secrets.token_urlsafe(32)
    jti = str(uuid.uuid4())
    expires = utcnow() + timedelta(seconds=settings.model_token_ttl_seconds)
    try:
        await store.create_model_token(device, body.task_id, raw, jti, body.model, expires)
    except PermissionError as error:
        raise HTTPException(403, "task is not active for this device") from error
    return ModelTokenResponse(access_token=raw, expires_in=settings.model_token_ttl_seconds, model=body.model)


@app.get("/tasks/{task_id}/approvals")
async def approvals(task_id: uuid.UUID, user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)):
    try:
        return await store.list_approvals(task_id, user)
    except PermissionError as error:
        raise HTTPException(404, "task not found") from error


@app.get("/tasks/{task_id}/audit")
async def task_audit(
    task_id: uuid.UUID,
    user: UserIdentity = Depends(current_user),
    store: Store = Depends(get_store),
):
    try:
        return await store.task_audit(task_id, user)
    except PermissionError as error:
        raise HTTPException(404, "task not found") from error


@app.post("/approvals/{approval_id}/decision")
async def approval_decision(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    user: UserIdentity = Depends(current_user),
    store: Store = Depends(get_store),
):
    if body.decision not in {"approved", "rejected"}:
        raise HTTPException(422, "invalid decision")
    try:
        approval, changed = await store.decide_approval(approval_id, user, body.decision)
    except PermissionError as error:
        raise HTTPException(404, "approval not found") from error
    if approval.decision_ack_at is None:
        try:
            payload = approval_delivery_payload(approval)
        except ValueError as error:
            raise HTTPException(409, "approval request id is not a JSON-RPC integer") from error
        await app.state.device_registry.send(approval.device_id, payload)
    return {"id": str(approval.id), "status": approval.status, "changed": changed}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID, user: UserIdentity = Depends(current_user), store: Store = Depends(get_store)
):
    try:
        task, changed = await store.cancel_task(task_id, user)
    except PermissionError as error:
        raise HTTPException(404, "task not found") from error
    if changed:
        await app.state.device_registry.send(task.device_id, {"type": "task.cancel", "task_id": str(task.id)})
    return {"id": str(task.id), "status": task.status, "changed": changed}


@app.post("/tasks/{task_id}/rollback")
async def rollback_task(
    task_id: uuid.UUID,
    user: UserIdentity = Depends(current_user),
    store: Store = Depends(get_store),
):
    try:
        rollback, created = await store.request_rollback(task_id, user)
    except PermissionError as error:
        raise HTTPException(404, "task not found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if rollback.acknowledged_at is None and rollback.status == "requested":
        await app.state.device_registry.send(rollback.device_id, rollback_delivery_payload(rollback))
    return {
        "task_id": str(rollback.task_id),
        "status": rollback.status,
        "delivery_id": rollback.delivery_id,
        "created": created,
    }


@app.post("/tasks", response_model=TaskOut)
async def create_task(
    body: CreateTaskRequest,
    response: Response,
    user: UserIdentity = Depends(current_user),
    store: Store = Depends(get_store),
) -> TaskOut:
    try:
        task, created = await store.create_task(
            user, body.device_id, body.project_id, body.conversation_id, body.idempotency_key, body.prompt
        )
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="project, device, or conversation is not available"
        ) from error
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
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

    return StreamingResponse(
        replay_and_follow(store, task, after_sequence, follow=follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        if raw_auth.get("type") == "pair":
            try:
                payload = LegacyPairPayload.model_validate(raw_auth.get("payload"))
                paired, credential, projects = await store.consume_pairing_code(
                    payload.code,
                    payload.name,
                    payload.publicKey,
                    payload.version,
                    [root.label for root in payload.roots],
                )
            except (ValidationError, PermissionError):
                await websocket.send_json({"type": "pair_error", "payload": {"error": "配对码无效或已过期"}})
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            await websocket.send_json(
                {
                    "version": 1,
                    "messageId": str(uuid.uuid4()),
                    "type": "pair_result",
                    "payload": {
                        "deviceId": str(paired.id),
                        "credential": credential,
                        "roots": [{"id": project.root_id, "label": project.display_name} for project in projects],
                    },
                }
            )
            return
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
        for pending_approval in await store.pending_approval_decisions(device):
            try:
                await app.state.device_registry.send(device.id, approval_delivery_payload(pending_approval))
            except ValueError:
                await websocket.send_json({"type": "error", "code": "invalid_approval_request_id"})
        for pending_rollback in await store.pending_rollbacks(device):
            await app.state.device_registry.send(device.id, rollback_delivery_payload(pending_rollback))
        while True:
            raw = await websocket.receive_json()
            if raw.get("type") == "heartbeat":
                await store.touch_device(device, raw.get("runtime_version"))
                await websocket.send_json({"type": "heartbeat.ack"})
                continue
            if raw.get("type") == "task.dispatch.ack":
                try:
                    task_id = uuid.UUID(raw["task_id"])
                    delivery_id = str(raw["delivery_id"])
                except (KeyError, ValueError):
                    await websocket.send_json({"type": "error", "code": "invalid_delivery_ack"})
                    continue
                accepted = await store.acknowledge_delivery(device, task_id, delivery_id)
                await websocket.send_json(
                    {"type": "task.dispatch.acknowledged", "task_id": str(task_id), "accepted": accepted}
                )
                continue
            if raw.get("type") == "approval.decision.ack":
                try:
                    approval_id = uuid.UUID(raw["approval_id"])
                    delivery_id = str(raw["delivery_id"])
                except (KeyError, ValueError):
                    await websocket.send_json({"type": "error", "code": "invalid_approval_ack"})
                    continue
                accepted = await store.acknowledge_approval_decision(device, approval_id, delivery_id)
                await websocket.send_json(
                    {"type": "approval.decision.acknowledged", "approval_id": str(approval_id), "accepted": accepted}
                )
                continue
            if raw.get("type") == "task.rollback.ack":
                try:
                    task_id = uuid.UUID(raw["task_id"])
                    delivery_id = str(raw["delivery_id"])
                    rollback_status = str(raw["status"])
                except (KeyError, ValueError):
                    await websocket.send_json({"type": "error", "code": "invalid_rollback_ack"})
                    continue
                accepted = await store.acknowledge_rollback(device, task_id, delivery_id, rollback_status)
                await websocket.send_json(
                    {
                        "type": "task.rollback.acknowledged",
                        "task_id": str(task_id),
                        "accepted": accepted,
                    }
                )
                continue
            message = DeviceEventMessage.model_validate(raw)
            if message.type != "task.event":
                await websocket.send_json({"type": "error", "code": "unsupported_message"})
                continue
            try:
                event = await store.append_event(
                    device, message.task_id, message.source_event_id, message.event_type, message.payload
                )
            except PermissionError:
                await websocket.send_json({"type": "error", "code": "task_not_assigned"})
                continue
            await websocket.send_json(
                {
                    "type": "task.event.ack",
                    "task_id": str(event.task_id),
                    "source_event_id": event.source_event_id,
                    "sequence": event.sequence,
                }
            )
    except (WebSocketDisconnect, ValidationError):
        if websocket.client_state.name.lower() != "disconnected":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    finally:
        if device:
            await app.state.device_registry.unregister(device.id, websocket)
        if isinstance(store, PostgresStore):
            await store.session.close()
