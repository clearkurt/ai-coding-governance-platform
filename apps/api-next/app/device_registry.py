import asyncio
import uuid

from fastapi import WebSocket

from app.store import TaskIdentity


class DeviceConnectionRegistry:
    """In-process routing for the first single-instance deployment.

    It is intentionally replaceable before multi-instance deployment (Redis/NATS is not
    introduced in this phase). Delivery IDs are persisted on tasks, so reconnect delivery
    remains idempotent even when this in-memory registry is lost on an API restart.
    """

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def register(self, device_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[device_id] = websocket

    async def unregister(self, device_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(device_id) is websocket:
                self._connections.pop(device_id, None)

    async def deliver(self, task: TaskIdentity) -> bool:
        async with self._lock:
            websocket = self._connections.get(task.device_id)
        if not websocket:
            return False
        await websocket.send_json({
            "type": "task.dispatch",
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "conversation_id": str(task.conversation_id),
            "root_id": task.root_id,
            "prompt": task.prompt,
            "delivery_id": task.delivery_id,
        })
        return True
