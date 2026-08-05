"""create target control plane schema

Revision ID: 20260804_0001
Revises:
Create Date: 2026-08-04 00:00:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260804_0001"
down_revision = None
branch_labels = None
depends_on = None

TASK_STATUS_VALUES = ("pending", "running", "waiting_approval", "completed", "failed", "cancelled")
TASK_STATUS = postgresql.ENUM(*TASK_STATUS_VALUES, name="task_status", create_type=False)


def identity_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    TASK_STATUS.create(bind, checkfirst=False)
    op.create_table(
        "teams",
        *identity_columns(),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "users",
        *identity_columns(),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("role", sa.String(32), server_default="member", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "team_id", name="uq_users_id_team"),
        sa.UniqueConstraint("team_id", "email", name="uq_users_team_email"),
    )
    op.create_table(
        "sessions",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_table(
        "pairing_codes",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["creator_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("code_hash", name="uq_pairing_codes_code_hash"),
    )
    op.create_table(
        "devices",
        *identity_columns(),
        sa.Column("machine_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("runtime_version", sa.String(80)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "team_id", name="uq_devices_id_team"),
        sa.UniqueConstraint("team_id", "machine_id", name="uq_devices_team_machine"),
    )
    op.create_table(
        "device_credentials",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("credential_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("credential_hash", name="uq_device_credentials_hash"),
    )
    op.create_table(
        "projects",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("root_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "team_id", name="uq_projects_id_team"),
        sa.UniqueConstraint("team_id", "device_id", "root_id", name="uq_projects_team_device_root"),
    )
    op.create_table(
        "project_grants",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("access_level", sa.String(32), server_default="write", nullable=False),
        sa.ForeignKeyConstraint(["project_id", "team_id"], ["projects.id", "projects.team_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("team_id", "project_id", "user_id", name="uq_project_grants_team_project_user"),
    )
    op.create_table(
        "conversations",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(300), server_default="New conversation", nullable=False),
        sa.ForeignKeyConstraint(["owner_id", "team_id"], ["users.id", "users.team_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "team_id", name="uq_conversations_id_team"),
    )
    op.create_table(
        "codex_threads",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider_thread_id", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("conversation_id", name="uq_codex_threads_conversation"),
        sa.UniqueConstraint("provider_thread_id", name="uq_codex_threads_provider_id"),
        sa.UniqueConstraint("id", "team_id", name="uq_codex_threads_id_team"),
    )
    op.create_table(
        "turns",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("provider_turn_id", sa.String(255)),
        sa.Column("prompt_summary", sa.Text()),
        sa.ForeignKeyConstraint(
            ["thread_id", "team_id"], ["codex_threads.id", "codex_threads.team_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("thread_id", "sequence", name="uq_turns_thread_sequence"),
        sa.UniqueConstraint("id", "team_id", name="uq_turns_id_team"),
    )
    op.create_table(
        "tasks",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid()),
        sa.Column("root_id", sa.String(128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("delivery_ack_at", sa.DateTime(timezone=True)),
        sa.Column("rollback_delivery_id", sa.String(128)),
        sa.Column("rollback_requested_at", sa.DateTime(timezone=True)),
        sa.Column("rollback_ack_at", sa.DateTime(timezone=True)),
        sa.Column("rollback_status", sa.String(32)),
        sa.Column("status", TASK_STATUS, server_default="pending", nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "team_id"], ["projects.id", "projects.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["turn_id", "team_id"], ["turns.id", "turns.team_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "team_id", name="uq_tasks_id_team"),
        sa.UniqueConstraint("team_id", "device_id", "idempotency_key", name="uq_tasks_team_device_idempotency"),
    )
    op.create_table(
        "task_events",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_events_task_sequence"),
        sa.UniqueConstraint("task_id", "source_event_id", name="uq_task_events_task_source_event"),
    )
    op.create_index("ix_task_events_task_sequence", "task_events", ["task_id", "sequence"])
    op.create_table(
        "approval_requests",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("provider_item_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("decision_delivery_id", sa.String(128)),
        sa.Column("decision_ack_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "provider_item_id", name="uq_approval_requests_task_item"),
    )
    op.create_table(
        "model_tokens",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["device_id", "team_id"], ["devices.id", "devices.team_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id", "team_id"], ["users.id", "users.team_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("jti", name="uq_model_tokens_jti"),
        sa.UniqueConstraint("token_hash", name="uq_model_tokens_hash"),
    )
    op.create_table(
        "model_usage",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("provider_request_id", sa.String(255), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["task_id", "team_id"], ["tasks.id", "tasks.team_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("provider_request_id", name="uq_model_usage_provider_request"),
    )
    op.create_table(
        "audit_events",
        *identity_columns(),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("device_id", sa.Uuid()),
        sa.Column("task_id", sa.Uuid()),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )
    op.create_index("ix_audit_events_team_created", "audit_events", ["team_id", "created_at"])
    op.create_table(
        "runtime_releases",
        *identity_columns(),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("codex_version", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.String(80), nullable=False),
        sa.Column("model_catalog_version", sa.String(80), nullable=False),
        sa.Column("config_template_version", sa.String(80), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("version", name="uq_runtime_releases_version"),
    )


def downgrade() -> None:
    op.drop_table("runtime_releases")
    op.drop_index("ix_audit_events_team_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("model_usage")
    op.drop_table("model_tokens")
    op.drop_table("approval_requests")
    op.drop_index("ix_task_events_task_sequence", table_name="task_events")
    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("turns")
    op.drop_table("codex_threads")
    op.drop_table("conversations")
    op.drop_table("project_grants")
    op.drop_table("projects")
    op.drop_table("device_credentials")
    op.drop_table("devices")
    op.drop_table("pairing_codes")
    op.drop_table("sessions")
    op.drop_table("users")
    op.drop_table("teams")
    TASK_STATUS.drop(op.get_bind(), checkfirst=False)
