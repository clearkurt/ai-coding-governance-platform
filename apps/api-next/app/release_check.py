import asyncio

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.schema import has_target_schema
from app.preflight import validate_configuration
from app.settings import Settings


async def check_online_schema(settings: Settings) -> bool:
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            return await has_target_schema(connection)
    finally:
        await engine.dispose()


def main() -> int:
    valid, messages = validate_configuration()
    if not valid:
        print("release check failed: production configuration invalid")
        for message in messages:
            print(f"- {message}")
        return 1
    settings = Settings()
    try:
        schema_ready = asyncio.run(check_online_schema(settings))
    except (OSError, SQLAlchemyError):
        print("release check failed: database unavailable")
        return 1
    if not schema_ready:
        print("release check failed: database schema unsupported")
        return 1
    print(f"release check passed (rollout mode: {settings.codex_rollout_mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
