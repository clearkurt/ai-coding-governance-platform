import ast
import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models import Base

MIGRATION = Path(__file__).parents[1] / "alembic" / "versions" / "20260804_0001_initial_control_plane.py"


def load_migration():
    spec = importlib.util.spec_from_file_location("initial_control_plane", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_tables() -> set[str]:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    return {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "create_table"
        and call.args
        and isinstance(call.args[0], ast.Constant)
    }


def test_initial_migration_is_explicit_and_exactly_matches_models() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for forbidden in ("from app.db.models", "Base.metadata", "create_all", "drop_all"):
        assert forbidden not in source
    assert migration_tables() == set(Base.metadata.tables)


def test_initial_migration_compiles_as_postgresql_offline_sql() -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output, "transactional_ddl": True},
    )
    migration = load_migration()
    migration.op = Operations(context)
    migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TYPE task_status AS ENUM" in sql
    assert "CREATE TABLE pairing_codes" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "JSON" in sql
    assert "UUID" in sql
    assert "decision_delivery_id" in sql
    assert "rollback_delivery_id" in sql


def test_downgrade_drops_tables_in_reverse_and_removes_enum() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    downgrade = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "downgrade")
    dropped = [
        call.args[0].value
        for call in ast.walk(downgrade)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "drop_table"
        and isinstance(call.args[0], ast.Constant)
    ]
    assert dropped[0] == "runtime_releases" and dropped[-1] == "teams"
    assert "TASK_STATUS.drop" in source
