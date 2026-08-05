from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    pass


class IdTimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TeamScopedMixin:
    team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class TaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting_approval = "waiting_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Team(IdTimestampMixin, Base):
    __tablename__ = "teams"
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class User(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("id", "team_id", name="uq_users_id_team"),
        UniqueConstraint("team_id", "email", name="uq_users_team_email"),
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False)


class Session(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PairingCode(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "pairing_codes"
    __table_args__ = (
        ForeignKeyConstraint(["creator_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        UniqueConstraint("code_hash", name="uq_pairing_codes_code_hash"),
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Device(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("id", "team_id", name="uq_devices_id_team"),
        UniqueConstraint("team_id", "machine_id", name="uq_devices_team_machine"),
    )
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    runtime_version: Mapped[str | None] = mapped_column(String(80))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False)


class DeviceCredential(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "device_credentials"
    __table_args__ = (
        ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        UniqueConstraint("credential_hash", name="uq_device_credentials_hash"),
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        UniqueConstraint("id", "team_id", name="uq_projects_id_team"),
        UniqueConstraint("team_id", "device_id", "root_id", name="uq_projects_team_device_root"),
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    root_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class ProjectGrant(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "project_grants"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "team_id"], ["projects.id", "projects.team_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        UniqueConstraint("team_id", "project_id", "user_id", name="uq_project_grants_team_project_user"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    access_level: Mapped[str] = mapped_column(String(32), nullable=False, default="write")


class Conversation(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "team_id"], ["users.id", "users.team_id"], ondelete="RESTRICT"),
        UniqueConstraint("id", "team_id", name="uq_conversations_id_team"),
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="New conversation")


class CodexThread(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "codex_threads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"], ondelete="CASCADE"
        ),
        UniqueConstraint("conversation_id", name="uq_codex_threads_conversation"),
        UniqueConstraint("provider_thread_id", name="uq_codex_threads_provider_id"),
        UniqueConstraint("id", "team_id", name="uq_codex_threads_id_team"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)


class Turn(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "team_id"], ["codex_threads.id", "codex_threads.team_id"], ondelete="CASCADE"
        ),
        UniqueConstraint("thread_id", "sequence", name="uq_turns_thread_sequence"),
        UniqueConstraint("id", "team_id", name="uq_turns_id_team"),
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_turn_id: Mapped[str | None] = mapped_column(String(255))
    prompt_summary: Mapped[str | None] = mapped_column(Text)


class Task(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["project_id", "team_id"], ["projects.id", "projects.team_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(["turn_id", "team_id"], ["turns.id", "turns.team_id"], ondelete="RESTRICT"),
        UniqueConstraint("id", "team_id", name="uq_tasks_id_team"),
        UniqueConstraint("team_id", "device_id", "idempotency_key", name="uq_tasks_team_device_idempotency"),
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    root_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False, default=lambda: str(uuid.uuid4()))
    delivery_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_delivery_id: Mapped[str | None] = mapped_column(String(128))
    rollback_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_status: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.pending
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskEvent(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "task_events"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        UniqueConstraint("task_id", "sequence", name="uq_task_events_task_sequence"),
        UniqueConstraint("task_id", "source_event_id", name="uq_task_events_task_source_event"),
        Index("ix_task_events_task_sequence", "task_id", "sequence"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class ApprovalRequest(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        UniqueConstraint("task_id", "provider_item_id", name="uq_approval_requests_task_item"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_delivery_id: Mapped[str | None] = mapped_column(String(128))
    decision_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelToken(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "model_tokens"
    __table_args__ = (
        ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        UniqueConstraint("jti", name="uq_model_tokens_jti"),
        UniqueConstraint("token_hash", name="uq_model_tokens_hash"),
    )
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    jti: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelUsage(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="RESTRICT"),
        UniqueConstraint("provider_request_id", name="uq_model_usage_provider_request"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuditEvent(IdTimestampMixin, TeamScopedMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_team_created", "team_id", "created_at"),)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class RuntimeRelease(IdTimestampMixin, Base):
    __tablename__ = "runtime_releases"
    __table_args__ = (UniqueConstraint("version", name="uq_runtime_releases_version"),)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    codex_version: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_catalog_version: Mapped[str] = mapped_column(String(80), nullable=False)
    config_template_version: Mapped[str] = mapped_column(String(80), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
