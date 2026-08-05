import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.security import utcnow
from app.store import PostgresStore, Store, UserIdentity

SESSION_COOKIE = "company_session"
SESSION_TTL = timedelta(hours=12)


async def get_store(session: AsyncSession = Depends(get_session)) -> AsyncIterator[Store]:
    yield PostgresStore(session)


async def current_user(
    store: Store = Depends(get_store), company_session: str | None = Cookie(default=None)
) -> UserIdentity:
    if not company_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    user = await store.session_user(company_session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired or revoked")
    return user


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry():
    return utcnow() + SESSION_TTL
