from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ApprovalRequest,
    AuditEvent,
    Conversation,
    Device,
    DeviceCredential,
    ModelToken,
    ModelUsage,
    Project,
    ProjectGrant,
    Session,
    Task,
    TaskEvent,
    TaskStatus,
    User,
)
from app.security import hash_secret, utcnow

TERMINAL_EVENT_STATUSES = {
    "turn/completed": TaskStatus.completed,
    "turn/failed": TaskStatus.failed,
    "turn/cancelled": TaskStatus.cancelled,
}


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


@dataclass(frozen=True)
class ModelAuthorization:
    token_id: uuid.UUID
    team_id: uuid.UUID
    user_id: uuid.UUID
    device_id: uuid.UUID
    task_id: uuid.UUID
    model: str


@dataclass(frozen=True)
class ApprovalIdentity:
    id: uuid.UUID
    task_id: uuid.UUID
    team_id: uuid.UUID
    device_id: uuid.UUID
    provider_item_id: str
    status: str
    decision_delivery_id: str | None = None
    decision_ack_at: datetime | None = None


@dataclass(frozen=True)
class RollbackIdentity:
    task_id: uuid.UUID
    device_id: uuid.UUID
    root_id: str
    delivery_id: str
    status: str
    acknowledged_at: datetime | None = None


@dataclass(frozen=True)
class AuditIdentity:
    id: uuid.UUID
    event_type: str
    metadata: dict[str, object]
    created_at: datetime


class Store(Protocol):
    async def find_user(self, team_id: uuid.UUID, email: str) -> UserIdentity | None: ...
    async def create_session(self, user: UserIdentity, token: str, expires_at: datetime) -> None: ...
    async def revoke_session(self, token: str) -> None: ...
    async def session_user(self, token: str) -> UserIdentity | None: ...
    async def authenticate_device(self, device_id: uuid.UUID, credential: str) -> DeviceIdentity | None: ...
    async def touch_device(self, device: DeviceIdentity, runtime_version: str | None) -> None: ...
    async def create_task(
        self,
        user: UserIdentity,
        device_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID,
        idempotency_key: str,
        prompt: str,
    ) -> tuple[TaskIdentity, bool]: ...
    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None: ...
    async def append_event(
        self,
        device: DeviceIdentity,
        task_id: uuid.UUID,
        source_event_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> PersistedEvent: ...
    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]: ...
    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]: ...
    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool: ...
    async def create_model_token(
        self, device: DeviceIdentity, task_id: uuid.UUID, raw_token: str, jti: str, model: str, expires_at: datetime
    ) -> ModelAuthorization: ...
    async def validate_model_token(self, raw_token: str, model: str) -> ModelAuthorization | None: ...
    async def record_model_usage(
        self, auth: ModelAuthorization, provider_request_id: str, input_tokens: int, output_tokens: int
    ) -> bool: ...
    async def model_usage_total(self, team_id: uuid.UUID) -> int: ...
    async def list_approvals(self, task_id: uuid.UUID, user: UserIdentity) -> list[ApprovalIdentity]: ...
    async def decide_approval(
        self, approval_id: uuid.UUID, user: UserIdentity, decision: str
    ) -> tuple[ApprovalIdentity, bool]: ...
    async def cancel_task(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[TaskIdentity, bool]: ...
    async def pending_approval_decisions(self, device: DeviceIdentity) -> list[ApprovalIdentity]: ...
    async def acknowledge_approval_decision(
        self, device: DeviceIdentity, approval_id: uuid.UUID, delivery_id: str
    ) -> bool: ...
    async def request_rollback(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[RollbackIdentity, bool]: ...
    async def pending_rollbacks(self, device: DeviceIdentity) -> list[RollbackIdentity]: ...
    async def acknowledge_rollback(
        self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str, rollback_status: str
    ) -> bool: ...
    async def task_audit(self, task_id: uuid.UUID, user: UserIdentity) -> list[AuditIdentity]: ...


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
        return TaskIdentity(
            model.id,
            model.team_id,
            model.device_id,
            model.project_id,
            model.conversation_id,
            model.root_id,
            model.prompt,
            model.status.value,
            model.delivery_id,
        )

    async def find_user(self, team_id: uuid.UUID, email: str) -> UserIdentity | None:
        model = await self.session.scalar(select(User).where(User.team_id == team_id, User.email == email))
        return self._user(model) if model else None

    async def create_session(self, user: UserIdentity, token: str, expires_at: datetime) -> None:
        self.session.add(
            Session(team_id=user.team_id, user_id=user.id, token_hash=hash_secret(token), expires_at=expires_at)
        )
        await self.session.commit()

    async def revoke_session(self, token: str) -> None:
        model = await self.session.scalar(
            select(Session).where(Session.token_hash == hash_secret(token), Session.revoked_at.is_(None))
        )
        if model:
            model.revoked_at = utcnow()
            await self.session.commit()

    async def session_user(self, token: str) -> UserIdentity | None:
        now = utcnow()
        row = await self.session.execute(
            select(User)
            .join(Session, Session.user_id == User.id)
            .where(
                Session.token_hash == hash_secret(token),
                Session.revoked_at.is_(None),
                Session.expires_at > now,
                User.is_active.is_(True),
            )
        )
        model = row.scalar_one_or_none()
        return self._user(model) if model else None

    async def authenticate_device(self, device_id: uuid.UUID, credential: str) -> DeviceIdentity | None:
        now = utcnow()
        row = await self.session.execute(
            select(Device)
            .join(DeviceCredential, DeviceCredential.device_id == Device.id)
            .where(
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
        self,
        user: UserIdentity,
        device_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID,
        idempotency_key: str,
        prompt: str,
    ) -> tuple[TaskIdentity, bool]:
        device = await self.session.get(Device, device_id)
        project = await self.session.get(Project, project_id)
        conversation = await self.session.get(Conversation, conversation_id)
        grant = await self.session.scalar(
            select(ProjectGrant.id).where(
                ProjectGrant.team_id == user.team_id,
                ProjectGrant.project_id == project_id,
                ProjectGrant.user_id == user.id,
            )
        )
        if not device or not project or not conversation or not grant:
            raise PermissionError("resource is not available to this team member")
        if (
            any(item.team_id != user.team_id for item in (device, project, conversation))
            or project.device_id != device_id
        ):
            raise PermissionError("cross-team or cross-device task request")
        existing = await self.session.scalar(
            select(Task).where(
                Task.team_id == user.team_id, Task.device_id == device_id, Task.idempotency_key == idempotency_key
            )
        )
        if existing:
            return self._task(existing), False
        model = Task(
            team_id=user.team_id,
            device_id=device_id,
            project_id=project_id,
            conversation_id=conversation_id,
            root_id=project.root_id,
            prompt=prompt,
            idempotency_key=idempotency_key,
        )
        self.session.add(model)
        await self.session.flush()
        self.session.add(
            AuditEvent(
                team_id=user.team_id,
                actor_user_id=user.id,
                device_id=device.id,
                task_id=model.id,
                event_type="task.created",
                metadata_={"project_id": str(project.id), "root_id": project.root_id},
            )
        )
        await self.session.commit()
        return self._task(model), True

    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None:
        model = await self.session.scalar(select(Task).where(Task.id == task_id, Task.team_id == user.team_id))
        if not model:
            return None
        grant = await self.session.scalar(
            select(ProjectGrant.id).where(
                ProjectGrant.team_id == user.team_id,
                ProjectGrant.project_id == model.project_id,
                ProjectGrant.user_id == user.id,
            )
        )
        return self._task(model) if grant else None

    async def append_event(
        self,
        device: DeviceIdentity,
        task_id: uuid.UUID,
        source_event_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> PersistedEvent:
        task = await self.session.scalar(
            select(Task).where(Task.id == task_id, Task.team_id == device.team_id, Task.device_id == device.id)
        )
        if not task:
            raise PermissionError("task is not assigned to this device")
        # PostgreSQL row locking serializes sequence allocation for a task.
        await self.session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
        existing = await self.session.scalar(
            select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.source_event_id == source_event_id)
        )
        if existing:
            return PersistedEvent(
                task_id, existing.sequence, existing.source_event_id, existing.event_type, existing.payload
            )
        sequence = (
            await self.session.scalar(
                select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task_id)
            )
        ) + 1
        persisted_payload = dict(payload)
        if event_type.endswith("/requestApproval"):
            provider_item_id = str(payload.get("request_id") or payload.get("id") or source_event_id)
            approval = await self.session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.task_id == task_id, ApprovalRequest.provider_item_id == provider_item_id
                )
            )
            if not approval:
                approval = ApprovalRequest(
                    team_id=device.team_id, task_id=task_id, provider_item_id=provider_item_id, status="pending"
                )
                self.session.add(approval)
                await self.session.flush()
            persisted_payload["approval_id"] = str(approval.id)
            if task.status in {TaskStatus.pending, TaskStatus.running}:
                task.status = TaskStatus.waiting_approval
        event = TaskEvent(
            team_id=device.team_id,
            task_id=task_id,
            sequence=sequence,
            source_event_id=source_event_id,
            event_type=event_type,
            payload=persisted_payload,
        )
        self.session.add(event)
        terminal_status = TERMINAL_EVENT_STATUSES.get(event_type)
        if terminal_status and task.status not in {
            TaskStatus.completed,
            TaskStatus.failed,
            TaskStatus.cancelled,
        }:
            task.status = terminal_status
            task.completed_at = utcnow()
            tokens = await self.session.scalars(
                select(ModelToken).where(ModelToken.task_id == task_id, ModelToken.revoked_at.is_(None))
            )
            for token in tokens:
                token.revoked_at = utcnow()
        self.session.add(
            AuditEvent(
                team_id=device.team_id,
                device_id=device.id,
                task_id=task_id,
                event_type=f"codex.{event_type}",
                metadata_={"source_event_id": source_event_id, "sequence": sequence},
            )
        )
        await self.session.commit()
        return PersistedEvent(task_id, sequence, source_event_id, event_type, persisted_payload)

    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]:
        rows = await self.session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.team_id == task.team_id, TaskEvent.sequence > after_sequence)
            .order_by(TaskEvent.sequence)
        )
        return [PersistedEvent(task.id, row.sequence, row.source_event_id, row.event_type, row.payload) for row in rows]

    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]:
        rows = await self.session.scalars(
            select(Task).where(
                Task.team_id == device.team_id,
                Task.device_id == device.id,
                Task.status.in_([TaskStatus.pending, TaskStatus.running, TaskStatus.waiting_approval]),
            )
        )
        return [self._task(row) for row in rows]

    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool:
        task = await self.session.scalar(
            select(Task).where(Task.id == task_id, Task.team_id == device.team_id, Task.device_id == device.id)
        )
        if not task or task.delivery_id != delivery_id:
            return False
        task.delivery_ack_at = utcnow()
        if task.status == TaskStatus.pending:
            task.status = TaskStatus.running
        self.session.add(
            AuditEvent(
                team_id=device.team_id,
                device_id=device.id,
                task_id=task_id,
                event_type="task.delivery_acknowledged",
                metadata_={"delivery_id": delivery_id},
            )
        )
        await self.session.commit()
        return True

    async def create_model_token(
        self, device: DeviceIdentity, task_id: uuid.UUID, raw_token: str, jti: str, model: str, expires_at: datetime
    ) -> ModelAuthorization:
        task = await self.session.scalar(
            select(Task).where(
                Task.id == task_id,
                Task.team_id == device.team_id,
                Task.device_id == device.id,
                Task.status.in_([TaskStatus.pending, TaskStatus.running, TaskStatus.waiting_approval]),
            )
        )
        if not task:
            raise PermissionError("task is not active for this device")
        conversation = await self.session.get(Conversation, task.conversation_id)
        if not conversation or conversation.team_id != device.team_id:
            raise PermissionError("task conversation is unavailable")
        model_token = ModelToken(
            team_id=device.team_id,
            user_id=conversation.owner_id,
            device_id=device.id,
            task_id=task.id,
            jti=jti,
            token_hash=hash_secret(raw_token),
            model=model,
            expires_at=expires_at,
        )
        self.session.add(model_token)
        self.session.add(
            AuditEvent(
                team_id=device.team_id,
                device_id=device.id,
                task_id=task.id,
                event_type="model.token_issued",
                metadata_={"model": model, "expires_at": expires_at.isoformat()},
            )
        )
        await self.session.commit()
        return ModelAuthorization(model_token.id, device.team_id, conversation.owner_id, device.id, task.id, model)

    async def validate_model_token(self, raw_token: str, model: str) -> ModelAuthorization | None:
        now = utcnow()
        token = await self.session.scalar(
            select(ModelToken)
            .join(Task, Task.id == ModelToken.task_id)
            .where(
                ModelToken.token_hash == hash_secret(raw_token),
                ModelToken.model == model,
                ModelToken.expires_at > now,
                ModelToken.revoked_at.is_(None),
                Task.status.in_([TaskStatus.pending, TaskStatus.running, TaskStatus.waiting_approval]),
                Task.team_id == ModelToken.team_id,
                Task.device_id == ModelToken.device_id,
            )
        )
        return (
            ModelAuthorization(token.id, token.team_id, token.user_id, token.device_id, token.task_id, token.model)
            if token
            else None
        )

    async def record_model_usage(
        self, auth: ModelAuthorization, provider_request_id: str, input_tokens: int, output_tokens: int
    ) -> bool:
        statement = (
            pg_insert(ModelUsage)
            .values(
                team_id=auth.team_id,
                task_id=auth.task_id,
                provider_request_id=provider_request_id,
                model=auth.model,
                input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens),
            )
            .on_conflict_do_nothing(index_elements=[ModelUsage.provider_request_id])
        )
        result = await self.session.execute(statement)
        await self.session.commit()
        return bool(result.rowcount)

    async def model_usage_total(self, team_id: uuid.UUID) -> int:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        value = await self.session.scalar(
            select(func.coalesce(func.sum(ModelUsage.input_tokens + ModelUsage.output_tokens), 0)).where(
                ModelUsage.team_id == team_id,
                ModelUsage.created_at >= day_start,
            )
        )
        return int(value or 0)

    async def list_approvals(self, task_id: uuid.UUID, user: UserIdentity) -> list[ApprovalIdentity]:
        task = await self.get_task_for_user(task_id, user)
        if not task:
            raise PermissionError("task unavailable")
        rows = await self.session.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.task_id == task.id, ApprovalRequest.team_id == user.team_id)
            .order_by(ApprovalRequest.created_at)
        )
        return [
            ApprovalIdentity(
                row.id,
                row.task_id,
                row.team_id,
                task.device_id,
                row.provider_item_id,
                row.status,
                row.decision_delivery_id,
                row.decision_ack_at,
            )
            for row in rows
        ]

    async def decide_approval(
        self, approval_id: uuid.UUID, user: UserIdentity, decision: str
    ) -> tuple[ApprovalIdentity, bool]:
        row = await self.session.get(ApprovalRequest, approval_id)
        if not row:
            raise PermissionError("approval unavailable")
        task = await self.get_task_for_user(row.task_id, user)
        if not task or row.team_id != user.team_id:
            raise PermissionError("approval unavailable")
        changed = row.status == "pending"
        if changed:
            row.status = decision
            row.resolved_at = utcnow()
            row.decision_delivery_id = str(uuid.uuid4())
            row.decision_ack_at = None
            self.session.add(
                AuditEvent(
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    device_id=task.device_id,
                    task_id=row.task_id,
                    event_type=f"approval.{decision}",
                    metadata_={"approval_id": str(row.id)},
                )
            )
            await self.session.commit()
        return ApprovalIdentity(
            row.id,
            row.task_id,
            row.team_id,
            task.device_id,
            row.provider_item_id,
            row.status,
            row.decision_delivery_id,
            row.decision_ack_at,
        ), changed

    async def pending_approval_decisions(self, device: DeviceIdentity) -> list[ApprovalIdentity]:
        rows = await self.session.execute(
            select(ApprovalRequest, Task.device_id)
            .join(Task, Task.id == ApprovalRequest.task_id)
            .where(
                ApprovalRequest.team_id == device.team_id,
                Task.device_id == device.id,
                ApprovalRequest.status.in_(["approved", "rejected"]),
                ApprovalRequest.decision_delivery_id.is_not(None),
                ApprovalRequest.decision_ack_at.is_(None),
            )
        )
        return [
            ApprovalIdentity(
                row.id,
                row.task_id,
                row.team_id,
                device.id,
                row.provider_item_id,
                row.status,
                row.decision_delivery_id,
                row.decision_ack_at,
            )
            for row, _ in rows
        ]

    async def acknowledge_approval_decision(
        self, device: DeviceIdentity, approval_id: uuid.UUID, delivery_id: str
    ) -> bool:
        row = await self.session.scalar(
            select(ApprovalRequest)
            .join(Task, Task.id == ApprovalRequest.task_id)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.team_id == device.team_id,
                Task.device_id == device.id,
                ApprovalRequest.decision_delivery_id == delivery_id,
            )
        )
        if not row:
            return False
        if row.decision_ack_at is None:
            row.decision_ack_at = utcnow()
            await self.session.commit()
        return True

    async def request_rollback(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[RollbackIdentity, bool]:
        task = await self.get_task_for_user(task_id, user)
        if not task:
            raise PermissionError("task unavailable")
        row = await self.session.get(Task, task_id)
        if row.status not in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
            raise ValueError("only a terminal task can be rolled back")
        created = row.rollback_delivery_id is None or row.rollback_status == "failed"
        if created:
            row.rollback_delivery_id = str(uuid.uuid4())
            row.rollback_requested_at = utcnow()
            row.rollback_ack_at = None
            row.rollback_status = "requested"
            self.session.add(
                AuditEvent(
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    device_id=row.device_id,
                    task_id=row.id,
                    event_type="task.rollback_requested",
                    metadata_={"delivery_id": row.rollback_delivery_id},
                )
            )
            await self.session.commit()
        return (
            RollbackIdentity(
                row.id,
                row.device_id,
                row.root_id,
                row.rollback_delivery_id,
                row.rollback_status or "requested",
                row.rollback_ack_at,
            ),
            created,
        )

    async def pending_rollbacks(self, device: DeviceIdentity) -> list[RollbackIdentity]:
        rows = await self.session.scalars(
            select(Task).where(
                Task.team_id == device.team_id,
                Task.device_id == device.id,
                Task.rollback_delivery_id.is_not(None),
                Task.rollback_ack_at.is_(None),
                Task.rollback_status == "requested",
            )
        )
        return [
            RollbackIdentity(
                row.id,
                row.device_id,
                row.root_id,
                row.rollback_delivery_id,
                row.rollback_status,
                row.rollback_ack_at,
            )
            for row in rows
        ]

    async def acknowledge_rollback(
        self,
        device: DeviceIdentity,
        task_id: uuid.UUID,
        delivery_id: str,
        rollback_status: str,
    ) -> bool:
        if rollback_status not in {"succeeded", "failed"}:
            return False
        row = await self.session.scalar(
            select(Task).where(
                Task.id == task_id,
                Task.team_id == device.team_id,
                Task.device_id == device.id,
                Task.rollback_delivery_id == delivery_id,
            )
        )
        if not row:
            return False
        if row.rollback_ack_at is None:
            row.rollback_ack_at = utcnow()
            row.rollback_status = rollback_status
            self.session.add(
                AuditEvent(
                    team_id=device.team_id,
                    device_id=device.id,
                    task_id=row.id,
                    event_type=f"task.rollback_{rollback_status}",
                    metadata_={"delivery_id": delivery_id},
                )
            )
            await self.session.commit()
        return True

    async def task_audit(self, task_id: uuid.UUID, user: UserIdentity) -> list[AuditIdentity]:
        if not await self.get_task_for_user(task_id, user):
            raise PermissionError("task unavailable")
        rows = await self.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.task_id == task_id, AuditEvent.team_id == user.team_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return [AuditIdentity(row.id, row.event_type, row.metadata_, row.created_at) for row in rows]

    async def cancel_task(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[TaskIdentity, bool]:
        task = await self.get_task_for_user(task_id, user)
        if not task:
            raise PermissionError("task unavailable")
        row = await self.session.get(Task, task_id)
        changed = row.status in {TaskStatus.pending, TaskStatus.running, TaskStatus.waiting_approval}
        if changed:
            row.status = TaskStatus.cancelled
            row.completed_at = utcnow()
            self.session.add(
                AuditEvent(
                    team_id=user.team_id,
                    actor_user_id=user.id,
                    device_id=row.device_id,
                    task_id=row.id,
                    event_type="task.cancelled",
                    metadata_={},
                )
            )
            await self.session.commit()
        if changed:
            tokens = await self.session.scalars(
                select(ModelToken).where(ModelToken.task_id == task_id, ModelToken.revoked_at.is_(None))
            )
            for token in tokens:
                token.revoked_at = utcnow()
            await self.session.commit()
        return self._task(row), changed


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
    model_tokens: dict[str, tuple[ModelAuthorization, datetime, datetime | None]] = field(default_factory=dict)
    usage_requests: set[str] = field(default_factory=set)
    usage_total: dict[uuid.UUID, int] = field(default_factory=dict)
    approvals: dict[uuid.UUID, ApprovalIdentity] = field(default_factory=dict)
    rollbacks: dict[uuid.UUID, RollbackIdentity] = field(default_factory=dict)
    audit_events: dict[uuid.UUID, list[AuditIdentity]] = field(default_factory=dict)

    def _audit(self, task_id: uuid.UUID, event_type: str, metadata: dict[str, object] | None = None) -> None:
        self.audit_events.setdefault(task_id, []).append(
            AuditIdentity(uuid.uuid4(), event_type, metadata or {}, utcnow())
        )

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
        if (
            not credential_item
            or credential_item[0] != device_id
            or credential_item[1]
            and credential_item[1] <= utcnow()
            or credential_item[2] is not None
        ):
            return None
        device = self.devices.get(device_id)
        return device if device and device.revoked_at is None else None

    async def touch_device(self, device: DeviceIdentity, runtime_version: str | None) -> None:
        return None

    async def create_task(
        self,
        user: UserIdentity,
        device_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID,
        idempotency_key: str,
        prompt: str,
    ) -> tuple[TaskIdentity, bool]:
        device = self.devices.get(device_id)
        project = self.projects.get(project_id)
        if (
            not device
            or not project
            or self.conversations.get(conversation_id) != user.team_id
            or (user.id, project_id) not in self.grants
        ):
            raise PermissionError("resource is not available to this team member")
        if device.team_id != user.team_id or project[:2] != (user.team_id, device_id):
            raise PermissionError("cross-team or cross-device task request")
        key = (user.team_id, device_id, idempotency_key)
        if key in self.idempotency:
            return self.tasks[self.idempotency[key]], False
        task_id = uuid.uuid4()
        task = TaskIdentity(
            task_id,
            user.team_id,
            device_id,
            project_id,
            conversation_id,
            project[2],
            prompt,
            TaskStatus.pending.value,
            str(task_id),
        )
        self.tasks[task.id] = task
        self.idempotency[key] = task.id
        self._audit(task.id, "task.created", {"project_id": str(project_id), "root_id": project[2]})
        return task, True

    async def get_task_for_user(self, task_id: uuid.UUID, user: UserIdentity) -> TaskIdentity | None:
        task = self.tasks.get(task_id)
        return task if task and task.team_id == user.team_id and (user.id, task.project_id) in self.grants else None

    async def append_event(
        self,
        device: DeviceIdentity,
        task_id: uuid.UUID,
        source_event_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> PersistedEvent:
        task = self.tasks.get(task_id)
        if not task or task.team_id != device.team_id or task.device_id != device.id:
            raise PermissionError("task is not assigned to this device")
        events = self.events.setdefault(task_id, [])
        existing = next((event for event in events if event.source_event_id == source_event_id), None)
        if existing:
            return existing
        persisted_payload = dict(payload)
        if event_type.endswith("/requestApproval"):
            provider_item_id = str(payload.get("request_id") or payload.get("id") or source_event_id)
            if not any(
                item.task_id == task_id and item.provider_item_id == provider_item_id
                for item in self.approvals.values()
            ):
                approval_id = uuid.uuid4()
                self.approvals[approval_id] = ApprovalIdentity(
                    approval_id, task_id, device.team_id, device.id, provider_item_id, "pending"
                )
            approval = next(
                item
                for item in self.approvals.values()
                if item.task_id == task_id and item.provider_item_id == provider_item_id
            )
            persisted_payload["approval_id"] = str(approval.id)
            if task.status in {"pending", "running"}:
                task = TaskIdentity(
                    task.id,
                    task.team_id,
                    task.device_id,
                    task.project_id,
                    task.conversation_id,
                    task.root_id,
                    task.prompt,
                    "waiting_approval",
                    task.delivery_id,
                )
                self.tasks[task.id] = task
        event = PersistedEvent(task_id, len(events) + 1, source_event_id, event_type, persisted_payload)
        events.append(event)
        self._audit(task_id, f"codex.{event_type}", {"source_event_id": source_event_id})
        terminal_status = TERMINAL_EVENT_STATUSES.get(event_type)
        if terminal_status:
            task = TaskIdentity(
                task.id,
                task.team_id,
                task.device_id,
                task.project_id,
                task.conversation_id,
                task.root_id,
                task.prompt,
                terminal_status.value,
                task.delivery_id,
            )
            self.tasks[task.id] = task
            for token_hash, (auth, expires_at, revoked_at) in list(self.model_tokens.items()):
                if auth.task_id == task_id and revoked_at is None:
                    self.model_tokens[token_hash] = (auth, expires_at, utcnow())
        return event

    async def events_after(self, task: TaskIdentity, after_sequence: int) -> list[PersistedEvent]:
        return [event for event in self.events.get(task.id, []) if event.sequence > after_sequence]

    async def pending_tasks_for_device(self, device: DeviceIdentity) -> list[TaskIdentity]:
        return [
            task
            for task in self.tasks.values()
            if task.team_id == device.team_id
            and task.device_id == device.id
            and task.status in {"pending", "running", "waiting_approval"}
        ]

    async def acknowledge_delivery(self, device: DeviceIdentity, task_id: uuid.UUID, delivery_id: str) -> bool:
        task = self.tasks.get(task_id)
        accepted = bool(
            task and task.team_id == device.team_id and task.device_id == device.id and task.delivery_id == delivery_id
        )
        if accepted and task and task.status == "pending":
            self.tasks[task.id] = TaskIdentity(
                task.id,
                task.team_id,
                task.device_id,
                task.project_id,
                task.conversation_id,
                task.root_id,
                task.prompt,
                "running",
                task.delivery_id,
            )
            self._audit(task.id, "task.delivery_acknowledged", {"delivery_id": delivery_id})
        return accepted

    async def create_model_token(
        self, device: DeviceIdentity, task_id: uuid.UUID, raw_token: str, jti: str, model: str, expires_at: datetime
    ) -> ModelAuthorization:
        task = self.tasks.get(task_id)
        if (
            not task
            or task.device_id != device.id
            or task.team_id != device.team_id
            or task.status not in {"pending", "running", "waiting_approval"}
        ):
            raise PermissionError("task is not active for this device")
        user_id = next((user_id for user_id, project_id in self.grants if project_id == task.project_id), None)
        if not user_id:
            raise PermissionError("task has no owner")
        auth = ModelAuthorization(uuid.uuid4(), device.team_id, user_id, device.id, task.id, model)
        self.model_tokens[hash_secret(raw_token)] = (auth, expires_at, None)
        self._audit(task.id, "model.token_issued", {"model": model})
        return auth

    async def validate_model_token(self, raw_token: str, model: str) -> ModelAuthorization | None:
        item = self.model_tokens.get(hash_secret(raw_token))
        if not item or item[1] <= utcnow() or item[2] is not None or item[0].model != model:
            return None
        task = self.tasks.get(item[0].task_id)
        return item[0] if task and task.status in {"pending", "running", "waiting_approval"} else None

    async def record_model_usage(
        self, auth: ModelAuthorization, provider_request_id: str, input_tokens: int, output_tokens: int
    ) -> bool:
        if provider_request_id in self.usage_requests:
            return False
        self.usage_requests.add(provider_request_id)
        self.usage_total[auth.team_id] = (
            self.usage_total.get(auth.team_id, 0) + max(0, input_tokens) + max(0, output_tokens)
        )
        return True

    async def model_usage_total(self, team_id: uuid.UUID) -> int:
        return self.usage_total.get(team_id, 0)

    async def list_approvals(self, task_id: uuid.UUID, user: UserIdentity) -> list[ApprovalIdentity]:
        if not await self.get_task_for_user(task_id, user):
            raise PermissionError("task unavailable")
        return [item for item in self.approvals.values() if item.task_id == task_id and item.team_id == user.team_id]

    async def decide_approval(
        self, approval_id: uuid.UUID, user: UserIdentity, decision: str
    ) -> tuple[ApprovalIdentity, bool]:
        item = self.approvals.get(approval_id)
        if not item or not await self.get_task_for_user(item.task_id, user):
            raise PermissionError("approval unavailable")
        changed = item.status == "pending"
        if changed:
            item = ApprovalIdentity(
                item.id,
                item.task_id,
                item.team_id,
                item.device_id,
                item.provider_item_id,
                decision,
                str(uuid.uuid4()),
                None,
            )
            self.approvals[item.id] = item
            self._audit(item.task_id, f"approval.{decision}", {"approval_id": str(item.id)})
        return item, changed

    async def pending_approval_decisions(self, device: DeviceIdentity) -> list[ApprovalIdentity]:
        return [
            item
            for item in self.approvals.values()
            if item.team_id == device.team_id
            and item.device_id == device.id
            and item.status in {"approved", "rejected"}
            and item.decision_delivery_id is not None
            and item.decision_ack_at is None
        ]

    async def acknowledge_approval_decision(
        self, device: DeviceIdentity, approval_id: uuid.UUID, delivery_id: str
    ) -> bool:
        item = self.approvals.get(approval_id)
        if (
            not item
            or item.device_id != device.id
            or item.team_id != device.team_id
            or item.decision_delivery_id != delivery_id
        ):
            return False
        if item.decision_ack_at is None:
            self.approvals[approval_id] = ApprovalIdentity(
                item.id,
                item.task_id,
                item.team_id,
                item.device_id,
                item.provider_item_id,
                item.status,
                item.decision_delivery_id,
                utcnow(),
            )
        return True

    async def request_rollback(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[RollbackIdentity, bool]:
        task = await self.get_task_for_user(task_id, user)
        if not task:
            raise PermissionError("task unavailable")
        if task.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("only a terminal task can be rolled back")
        existing = self.rollbacks.get(task_id)
        if existing and existing.status != "failed":
            return existing, False
        rollback = RollbackIdentity(task.id, task.device_id, task.root_id, str(uuid.uuid4()), "requested")
        self.rollbacks[task.id] = rollback
        self._audit(task.id, "task.rollback_requested", {"delivery_id": rollback.delivery_id})
        return rollback, True

    async def pending_rollbacks(self, device: DeviceIdentity) -> list[RollbackIdentity]:
        return [
            rollback
            for rollback in self.rollbacks.values()
            if rollback.device_id == device.id and rollback.status == "requested" and rollback.acknowledged_at is None
        ]

    async def acknowledge_rollback(
        self,
        device: DeviceIdentity,
        task_id: uuid.UUID,
        delivery_id: str,
        rollback_status: str,
    ) -> bool:
        rollback = self.rollbacks.get(task_id)
        task = self.tasks.get(task_id)
        if (
            rollback_status not in {"succeeded", "failed"}
            or not rollback
            or not task
            or task.team_id != device.team_id
            or rollback.device_id != device.id
            or rollback.delivery_id != delivery_id
        ):
            return False
        if rollback.acknowledged_at is None:
            self.rollbacks[task_id] = RollbackIdentity(
                rollback.task_id,
                rollback.device_id,
                rollback.root_id,
                rollback.delivery_id,
                rollback_status,
                utcnow(),
            )
            self._audit(task_id, f"task.rollback_{rollback_status}", {"delivery_id": delivery_id})
        return True

    async def task_audit(self, task_id: uuid.UUID, user: UserIdentity) -> list[AuditIdentity]:
        if not await self.get_task_for_user(task_id, user):
            raise PermissionError("task unavailable")
        return self.audit_events.get(task_id, [])

    async def cancel_task(self, task_id: uuid.UUID, user: UserIdentity) -> tuple[TaskIdentity, bool]:
        task = await self.get_task_for_user(task_id, user)
        if not task:
            raise PermissionError("task unavailable")
        changed = task.status in {"pending", "running", "waiting_approval"}
        if changed:
            task = TaskIdentity(
                task.id,
                task.team_id,
                task.device_id,
                task.project_id,
                task.conversation_id,
                task.root_id,
                task.prompt,
                "cancelled",
                task.delivery_id,
            )
            self.tasks[task.id] = task
            for token_hash, (auth, expires_at, revoked_at) in list(self.model_tokens.items()):
                if auth.task_id == task_id and revoked_at is None:
                    self.model_tokens[token_hash] = (auth, expires_at, utcnow())
            self._audit(task.id, "task.cancelled")
        return task, changed
