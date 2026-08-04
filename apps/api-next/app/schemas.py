from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DeviceHello(BaseModel):
    protocol_version: str = Field(min_length=1, max_length=80)
    runtime_version: str = Field(min_length=1, max_length=80)


class TaskEventIn(BaseModel):
    source_event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any]
    emitted_at: datetime


class TaskEventOut(TaskEventIn):
    task_id: UUID
    sequence: int


class LoginRequest(BaseModel):
    team_id: UUID
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class CurrentUser(BaseModel):
    id: UUID
    team_id: UUID
    email: str


class CreateTaskRequest(BaseModel):
    device_id: UUID
    project_id: UUID
    conversation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)


class TaskOut(BaseModel):
    id: UUID
    team_id: UUID
    device_id: UUID
    project_id: UUID
    conversation_id: UUID
    status: str


class DeviceAuthentication(BaseModel):
    type: str
    device_id: UUID
    credential: str = Field(min_length=1, max_length=4096)
    runtime_version: str | None = Field(default=None, max_length=80)


class DeviceEventMessage(BaseModel):
    type: str
    task_id: UUID
    source_event_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any]
