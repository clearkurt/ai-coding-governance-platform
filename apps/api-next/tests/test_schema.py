from app.db.models import Base, Task, TaskEvent


EXPECTED_TABLES = {
    "teams", "users", "sessions", "devices", "device_credentials", "projects", "project_grants",
    "conversations", "codex_threads", "turns", "tasks", "task_events", "approval_requests",
    "model_tokens", "model_usage", "audit_events", "runtime_releases",
}


def test_initial_schema_covers_the_control_plane() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_task_events_are_ordered_and_idempotent_per_task() -> None:
    constraints = {constraint.name for constraint in TaskEvent.__table__.constraints}
    assert "uq_task_events_task_sequence" in constraints
    assert "uq_task_events_task_source_event" in constraints


def test_task_turn_foreign_key_does_not_null_the_team_scope() -> None:
    turn_fk = next(
        constraint
        for constraint in Task.__table__.foreign_key_constraints
        if {element.parent.name for element in constraint.elements} == {"turn_id", "team_id"}
    )
    assert turn_fk.ondelete == "RESTRICT"


def test_sensitive_columns_store_only_hashes() -> None:
    for table_name, column_name in (("sessions", "token_hash"), ("device_credentials", "credential_hash"), ("model_tokens", "token_hash")):
        assert column_name in Base.metadata.tables[table_name].columns
        assert "token" not in {column.name for column in Base.metadata.tables[table_name].columns if column.name != "token_hash"}
