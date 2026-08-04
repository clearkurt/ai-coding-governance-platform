import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.schema import TARGET_SCHEMA_REVISION, has_target_schema
from app.preflight import validate_configuration
from app.settings import Settings

FAILURE_AUDITS = ("workspace.sync.failed", "rollback.failed", "backup.failed")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def bounded_int(value: str, minimum: int, maximum: int) -> int:
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Emit a read-only Codex rollout aggregate report")
    result.add_argument("--window-hours", type=lambda value: bounded_int(value, 1, 720), default=24)
    result.add_argument("--stale-hours", type=lambda value: bounded_int(value, 1, 168), default=2)
    result.add_argument("--team", type=uuid.UUID)
    return result


async def collect_report(
    session: AsyncSession, settings: Settings, window_hours: int, stale_hours: int, team_id: uuid.UUID | None
) -> dict[str, object]:
    now = datetime.now(UTC)
    start, stale_before = now - timedelta(hours=window_hours), now - timedelta(hours=stale_hours)
    team_clause = " AND team_id = :team_id" if team_id else ""
    parameters = {"start": start, "stale": stale_before, "team_id": team_id}

    async def grouped(statement: str) -> dict[str, int]:
        rows = await session.execute(text(statement), parameters)
        return {str(key): int(value) for key, value in rows}

    task_statuses = await grouped(
        f"SELECT status::text, count(*) FROM tasks WHERE requested_at >= :start{team_clause} GROUP BY status"
    )
    stale = await grouped(
        f"SELECT status::text, count(*) FROM tasks WHERE requested_at < :stale "
        f"AND status::text IN ('pending','running','waiting_approval'){team_clause} GROUP BY status"
    )
    audit_failures = await grouped(
        f"SELECT event_type, count(*) FROM audit_events WHERE created_at >= :start "
        f"AND event_type IN ('workspace.sync.failed','rollback.failed','backup.failed'){team_clause} GROUP BY event_type"
    )

    async def scalar(statement: str) -> int:
        return int((await session.execute(text(statement), parameters)).scalar_one() or 0)

    pending_delivery = await scalar(
        f"SELECT count(*) FROM tasks WHERE delivery_ack_at IS NULL AND requested_at >= :start{team_clause}"
    )
    pending_approval = await scalar(
        f"SELECT count(*) FROM approval_requests WHERE decision_delivery_id IS NOT NULL "
        f"AND decision_ack_at IS NULL AND created_at >= :start{team_clause}"
    )
    pending_rollback = await scalar(
        f"SELECT count(*) FROM tasks WHERE rollback_delivery_id IS NOT NULL AND rollback_ack_at IS NULL "
        f"AND requested_at >= :start{team_clause}"
    )
    usage = (
        await session.execute(
            text(
                f"SELECT count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0) "
                f"FROM model_usage WHERE created_at >= :start{team_clause}"
            ),
            parameters,
        )
    ).one()
    sequence_gaps = await scalar(
        f"SELECT count(*) FROM (SELECT task_id FROM task_events WHERE created_at >= :start{team_clause} "
        "GROUP BY task_id HAVING min(sequence) <> 1 OR count(*) <> max(sequence)) gaps"
    )
    missing_terminal = await scalar(
        "SELECT count(*) FROM tasks t WHERE t.completed_at >= :start AND t.status::text IN "
        "('completed','failed','cancelled')"
        + (" AND t.team_id = :team_id" if team_id else "")
        + " AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.event_type IN "
        "('turn/completed','turn/failed','turn/cancelled','turn/canceled'))"
    )
    terminal_total = sum(task_statuses.get(status, 0) for status in TERMINAL_STATUSES)
    ratios = {
        status: (round(task_statuses.get(status, 0) / terminal_total, 6) if terminal_total else 0.0)
        for status in TERMINAL_STATUSES
    }
    return {
        "schema_revision": TARGET_SCHEMA_REVISION,
        "rollout_mode": settings.codex_rollout_mode,
        "window": {"start": start.isoformat(), "end": now.isoformat(), "hours": window_hours},
        "tasks_by_status": {
            status: task_statuses.get(status, 0)
            for status in (*TERMINAL_STATUSES, "pending", "running", "waiting_approval")
        },
        "terminal_ratios": ratios,
        "stale_active": {
            "threshold_hours": stale_hours,
            **{status: stale.get(status, 0) for status in ("pending", "running", "waiting_approval")},
        },
        "pending_deliveries": {"task": pending_delivery, "approval": pending_approval, "rollback": pending_rollback},
        "failure_audits": {name: audit_failures.get(name, 0) for name in FAILURE_AUDITS},
        "model_usage": {"requests": int(usage[0]), "input_tokens": int(usage[1]), "output_tokens": int(usage[2])},
        "audit_checks": {
            "task_event_sequence_gaps": sequence_gaps,
            "terminal_tasks_without_terminal_event": missing_terminal,
        },
        "team_filter_applied": team_id is not None,
    }


async def run_report(settings: Settings, arguments: argparse.Namespace) -> dict[str, object]:
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            if not await has_target_schema(session):
                raise RuntimeError("unsupported schema")
            return await collect_report(
                session, settings, arguments.window_hours, arguments.stale_hours, arguments.team
            )
    finally:
        await engine.dispose()


def main() -> int:
    arguments = parser().parse_args()
    valid, _ = validate_configuration()
    if not valid:
        print(json.dumps({"error": "production configuration invalid"}, sort_keys=True))
        return 1
    try:
        report = asyncio.run(run_report(Settings(), arguments))
    except (OSError, RuntimeError, SQLAlchemyError):
        print(json.dumps({"error": "rollout report unavailable"}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
