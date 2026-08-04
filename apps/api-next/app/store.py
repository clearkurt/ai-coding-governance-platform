from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Device, DeviceCredential, Project, ProjectGrant, Session, Task, TaskEvent, TaskStatus, User
from app.security import hash_secret, utcnow


@dataclass(frozen=True)
class UserIdentity:
    id: uuid.UUID
    team_id: uuid.UUID
    email: str
    password_hash: str | None
    is_active: bool = True


@dataclass(frozen=True)
class DeviceIdentity:
    id: uuid.UUID
    team_id: uuid.UUID
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class TaskIdentity:
    id: uuid.UUID
    team_id: uuid.UUID
    device_id: uuid.UUID
    project_id: uuid.UUID
    conversation_id: uuid.UUID
    root_id: str
    prompt: str
    status: str
    delivery_id: str


@dataclass(frozen=True)
class PersistedEvent:
    task_id: uuid.UUID
    sequence: int
    source_event_id: str
    event_type: str
    payload: dict[str, object]


class Store(Protocol):
    async def find_user(self, team_id: uuid.UUID, email: str) -> UserIdentity | None: ...
    async def create_session(self, user: UserIdentity, token: str, expires_at: datetime) -> None: ...
    async def revoke_session(self, token: str) -> None: ...
    async def session_user(self, token: str) -> UserIdentity | None: ...
    async def authenticate_device(self, device_id: uuid.UUID, credential: str) -> DeviceIdentity | None: ...
    async def touch_device(self, device: DeviceIdentity, runtime_version: str | None) -> None: ...
    async def create_task(
        self, user: UserIdentity, device_id: uuid.UUID, project_id: uuid.UUID, conversation_id: uuid.UUID, idempotency_key: str, prompt: str
    ) -> tuple[TaskIdentity, bool]: ...
    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None: ...
    async def append_event(self, device: DeviceIdentity, task_id: uuid.UUID, source_event_id: str, event_type: str, payload: dict[str, object]) -> PersistedEvent: ...
    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]: ...
    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]: ...
    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool: ...


class PostgresStore:
    """The production adapter. It deliberately uses only the PostgreSQL SQLAlchemy session."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _user(model: User) -> UserIdentity:
        return UserIdentity(model.id, model.team_id, model.email, model.password_hash, model.is_active)

    @staticmethod
    def _device(model: Device) -> DeviceIdentity:
        return DeviceIdentity(model.id, model.team_id, model.revoked_at)

    @staticmethod
    def _task(model: Task) -> TaskIdentity:
        return TaskIdentity(model.id, model.team_id, model.device_id, model.project_id, model.conversation_id, model.root_id, model.prompt, model.status.value, model.delivery_id)

    async def find_user(self, team_id: uuid.UUID, email: str) -> UserIdentity | None:
        model = await self.session.scalar(select(User).where(User.team_id == team_id, User.email == email))
        return self._user(model) if model else None

    async def create_session(self, user: UserIdentity, token: str, expires_at: datetime) -> None:
        self.session.add(Session(team_id=user.team_id, user_id=user.id, token_hash=hash_secret(token), expires_at=expires_at))
        await self.session.commit()

    async def revoke_session(self, token: str) -> None:
        model = await self.session.scalar(select(Session).where(Session.token_hash == hash_secret(token), Session.revoked_at.is_(None)))
        if model:
            model.revoked_at = utcnow()
            await self.session.commit()

    async def session_user(self, token: str) -> UserIdentity | None:
        now = utcnow()
        row = await self.session.execute(
            select(User).join(Session, Session.user_id == User.id).where(
                Session.token_hash == hash_secret(token), Session.revoked_at.is_(None), Session.expires_at > now, User.is_active.is_(True)
            )
        )
        model = row.scalar_one_or_none()
        return self._user(model) if model else None

    async def authenticate_device(self, device_id: uuid.UUID, credential: str) -> DeviceIdentity | None:
        now = utcnow()
        row = await self.session.execute(
            select(Device).join(DeviceCredential, DeviceCredential.device_id == Device.id).where(
                Device.id == device_id,
                Device.revoked_at.is_(None),
                DeviceCredential.credential_hash == hash_secret(credential),
                DeviceCredential.revoked_at.is_(None),
                (DeviceCredential.expires_at.is_(None) | (DeviceCredential.expires_at > now)),
            )
        )
        model = row.scalar_one_or_none()
        return self._device(model) if model else None

    async def touch_device(self, device: DeviceIdentity, runtime_version: str | None) -> None:
        model = await self.session.get(Device, device.id)
        if model is None or model.team_id != device.team_id or model.revoked_at is not None:
            return
        model.last_seen_at = utcnow()
        if runtime_version:
            model.runtime_version = runtime_version
        await self.session.commit()

    async def create_task(
        self, user: UserIdentity, device_id: uuid.UUID, project_id: uuid.UUID, conversation_id: uuid.UUID, idempotency_key: str, prompt: str
    ) -> tuple[TaskIdentity, bool]:
        device = await self.session.get(Device, device_id)
        project = await self.session.get(Project, project_id)
        conversation = await self.session.get(Conversation, conversation_id)
        grant = await self.session.scalar(
            select(ProjectGrant.id).where(ProjectGrant.team_id == user.team_id, ProjectGrant.project_id == project_id, ProjectGrant.user_id == user.id)
        )
        if not device or not project or not conversation or not grant:
            raise PermissionError("resource is not available to this team member")
        if any(item.team_id != user.team_id for item in (device, project, conversation)) or project.device_id != device_id:
            raise PermissionError("cross-team or cross-device task request")
        existing = await self.session.scalar(
            select(Task).where(Task.team_id == user.team_id, Task.device_id == device_id, Task.idempotency_key == idempotency_key)
        )
        if existing:
            return self._task(existing), False
        model = Task(team_id=user.team_id, device_id=device_id, project_id=project_id, conversation_id=conversation_id, root_id=project.root_id, prompt=prompt, idempotency_key=idempotency_key)
        self.session.add(model)
        await self.session.commit()
        return self._task(model), True

    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None:
        model = await self.session.scalar(select(Task).where(Task.id == task_id, Task.team_id == user.team_id))
        if not model:
            return None
        grant = await self.session.scalar(
            select(ProjectGrant.id).where(ProjectGrant.team_id == user.team_id, ProjectGrant.project_id == model.project_id, ProjectGrant.user_id == user.id)
        )
        return self._task(model) if grant else None

    async def append_event(self, device: DeviceIdentity, task_id: uuid.UUID, source_event_id: str, event_type: str, payload: dict[str, object]) -> PersistedEvent:
        task = await self.session.scalar(select(Task).where(Task.id == task_id, Task.team_id == device.team_id, Task.device_id == device.id))
        if not task:
            raise PermissionError("task is not assigned to this device")
        # PostgreSQL row locking serializes sequence allocation for a task.
        await self.session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
        existing = await self.session.scalar(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.source_event_id == source_event_id))
        if existing:
            return PersistedEvent(task_id, existing.sequence, existing.source_event_id, existing.event_type, existing.payload)
        sequence = (await self.session.scalar(select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task_id))) + 1
        event = TaskEvent(team_id=device.team_id, task_id=task_id, sequence=sequence, source_event_id=source_event_id, event_type=event_type, payload=payload)
        self.session.add(event)
        await self.session.commit()
        return PersistedEvent(task_id, sequence, source_event_id, event_type, payload)

    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]:
        rows = await self.session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.team_id == task.team_id, TaskEvent.sequence > after_sequence).order_by(TaskEvent.sequence)
        )
        return [PersistedEvent(task.id, row.sequence, row.source_event_id, row.event_type, row.payload) for row in rows]

    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]:
        rows = await self.session.scalars(
            select(Task).where(Task.team_id == device.team_id, Task.device_id == device.id, Task.status.in_([TaskStatus.pending, TaskStatus.running]))
        )
        return [self._task(row) for row in rows]

    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool:
        task = await self.session.scalar(select(Task).where(Task.id == task_id, Task.team_id == device.team_id, Task.device_id == device.id))
        if not task or task.delivery_id != delivery_id:
            return False
        task.delivery_ack_at = utcnow()
        await self.session.commit()
        return True


@dataclass
class MemoryStore:
    """Test-only dependency replacement; production never selects this adapter."""

    users: dict[tuple[uuid.UUID, str], UserIdentity] = field(default_factory=dict)
    sessions: dict[str, tuple[UserIdentity, datetime, datetime | None]] = field(default_factory=dict)
    devices: dict[uuid.UUID, DeviceIdentity] = field(default_factory=dict)
    credentials: dict[str, tuple[uuid.UUID, datetime | None, datetime | None]] = field(default_factory=dict)
    grants: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    projects: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, str]] = field(default_factory=dict)
    conversations: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    tasks: dict[uuid.UUID, TaskIdentity] = field(default_factory=dict)
    idempotency: dict[tuple[uuid.UUID, uuid.UUID, str], uuid.UUID] = field(default_factory=dict)
    events: dict[uuid.UUID, list[PersistedEvent]] = field(default_factory=dict)

    async def find_user(self, team_id: uuid.UUID, email: str) -> UserIdentity | None:
        return self.users.get((team_id, email))

    async def create_session(self, user: UserIdentity, token: str, expires_at: datetime) -> None:
        self.sessions[hash_secret(token)] = (user, expires_at, None)

    async def revoke_session(self, token: str) -> None:
        item = self.sessions.get(hash_secret(token))
        if item:
            self.sessions[hash_secret(token)] = (item[0], item[1], utcnow())

    async def session_user(self, token: str) -> UserIdentity | None:
        item = self.sessions.get(hash_secret(token))
        if not item or item[1] <= utcnow() or item[2] is not None or not item[0].is_active:
            return None
        return item[0]

    async def authenticate_device(self, device_id: uuid.UUID, credential: str) -> DeviceIdentity | None:
        credential_item = self.credentials.get(hash_secret(credential))
        if not credential_item or credential_item[0] != device_id or credential_item[1] and credential_item[1] <= utcnow() or credential_item[2] is not None:
            return None
        device = self.devices.get(device_id)
        return device if device and device.revoked_at is None else None

    async def touch_device(self, device: DeviceIdentity, runtime_version: str | None) -> None:
        return None

    async def create_task(self, user: UserIdentity, device_id: uuid.UUID, project_id: uuid.UUID, conversation_id: uuid.UUID, idempotency_key: str, prompt: str) -> tuple[TaskIdentity, bool]:
        device = self.devices.get(device_id)
        project = self.projects.get(project_id)
        if not device or not project or self.conversations.get(conversation_id) != user.team_id or (user.id, project_id) not in self.grants:
            raise PermissionError("resource is not available to this team member")
        if device.team_id != user.team_id or project[:2] != (user.team_id, device_id):
            raise PermissionError("cross-team or cross-device task request")
        key = (user.team_id, device_id, idempotency_key)
        if key in self.idempotency:
            return self.tasks[self.idempotency[key]], False
        task_id = uuid.uuid4()
        task = TaskIdentity(task_id, user.team_id, device_id, project_id, conversation_id, project[2], prompt, TaskStatus.pending.value, str(task_id))
        self.tasks[task.id] = task
        self.idempotency[key] = task.id
        return task, True

    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None:
        task = self.tasks.get(task_id)
        return task if task and task.team_id == user.team_id and (user.id, task.project_id) in self.grants else None

    async def append_event(self, device: DeviceIdentity, task_id: uuid.UUID, source_event_id: str, event_type: str, payload: dict[str, object]) -> PersistedEvent:
        task = self.tasks.get(task_id)
        if not task or task.team_id != device.team_id or task.device_id != device.id:
            raise PermissionError("task is not assigned to this device")
        events = self.events.setdefault(task_id, [])
        existing = next((event for event in events if event.source_event_id == source_event_id), None)
        if existing:
            return existing
        event = PersistedEvent(task_id, len(events) + 1, source_event_id, event_type, payload)
        events.append(event)
        return event

    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]:
        return [event for event in self.events.get(task.id, []) if event.sequence > after_sequence]

    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]:
        return [task for task in self.tasks.values() if task.team_id == device.team_id and task.device_id == device.id and task.status in {"pending", "running"}]

    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool:
        task = self.tasks.get(task_id)
        return bool(task and task.team_id == device.team_id and task.device_id == device.id and task.delivery_id == delivery_id)
