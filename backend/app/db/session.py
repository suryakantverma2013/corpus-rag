"""Async engine + session factory (T-102).

One process-wide `AsyncEngine`/`async_sessionmaker` built from
`get_settings().database.url`. `get_session` is the FastAPI dependency: it yields a
session and guarantees close/rollback of anything uncommitted. Repositories never
commit — the caller (API route or worker task) owns the transaction boundary, which
also makes transactional-rollback unit tests trivial.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (cached)."""
    settings = get_settings()
    return create_async_engine(settings.database.url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory bound to the app engine."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an `AsyncSession` (caller commits)."""
    async with get_sessionmaker()() as session:
        yield session


def get_stream_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """The session **factory**, for handlers that outlive a request (T-210, R-41(7)).

    `get_session` is wrong for an SSE stream: it yields one session and holds it until the
    handler returns, which for a stream means until the user closes the modal. A pool slot
    per open viewer would starve ordinary requests. Streams therefore take the factory and
    open a short-lived session per poll tick.

    A thin wrapper over `get_sessionmaker` rather than that function used directly, so it
    is a distinct dependency key — tests override this one to bind the stream to the same
    transaction-scoped session the rest of the suite uses, without redirecting every other
    route's session at the same time.
    """
    return get_sessionmaker()
