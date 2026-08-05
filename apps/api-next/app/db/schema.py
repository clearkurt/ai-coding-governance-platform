from typing import Protocol

from sqlalchemy import text

TARGET_SCHEMA_REVISION = "20260804_0001"


class AsyncExecutor(Protocol):
    async def execute(self, statement): ...


async def has_target_schema(executor: AsyncExecutor) -> bool:
    result = await executor.execute(text("SELECT version_num FROM alembic_version"))
    revisions = list(result.scalars())
    return revisions == [TARGET_SCHEMA_REVISION]
