"""The R-24 processing-lock seam (T-302, R-43).

Two backends, but **not** the usual configuration-selected pair. There is no
`GRAPH_LOCK_BACKEND` setting and no production-refusal rule, because
:class:`MemoryProcessingLockStore` is reachable only by injection through
:class:`~app.rag.state.RAGContext` — which is strictly stronger than a refusal rule, since
there is no setting a deployment could get wrong. The gate the API serves is always the
database.

Only the *write* side lives here. The read side is
`ProcessingLockRepository.active_for(...)` called directly from `app.services.documents`,
which already owns a request-scoped session; routing it through a seam would open a second
transaction to answer a question the request's own one can answer.

Why the store opens its own session: a graph run outlives its HTTP request while streaming
and has no request at all on resume, so nodes take short-lived sessions from
`RAGContext.sessionmaker` and commit them (the rule stated at the top of `app.rag.graph`).
The lock write must also be **visible to other processes immediately** — an uncommitted row
gates nothing — which is why this is one of the few places in the codebase where a service
commits rather than leaving it to a caller.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.db.repositories.processing_lock import ProcessingLockRepository

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "DatabaseProcessingLockStore",
    "MemoryProcessingLockStore",
    "ProcessingLockStore",
    "new_token",
]


def new_token() -> str:
    """A release token. Unguessable is not the point — *unique* is.

    The token exists so an older turn's `finalize` cannot free a newer turn's gate, so all
    it must guarantee is that two turns never collide. `token_urlsafe(24)` fits the
    `String(64)` column with room to spare.
    """
    return secrets.token_urlsafe(24)


@runtime_checkable
class ProcessingLockStore(Protocol):
    """Write side of the R-24 gate. Both methods are best-effort by contract."""

    async def acquire(
        self, *, owner_id: uuid.UUID, conversation_id: uuid.UUID | None, token: str
    ) -> None: ...

    async def release(self, *, owner_id: uuid.UUID, token: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class DatabaseProcessingLockStore:
    """The real gate: one short transaction per call against `processing_locks`."""

    sessionmaker: async_sessionmaker[AsyncSession]
    ttl_seconds: float

    async def acquire(
        self, *, owner_id: uuid.UUID, conversation_id: uuid.UUID | None, token: str
    ) -> None:
        async with self.sessionmaker() as session:
            await ProcessingLockRepository(session).acquire(
                owner_id=owner_id,
                conversation_id=conversation_id,
                token=token,
                ttl_seconds=self.ttl_seconds,
            )
            await session.commit()

    async def release(self, *, owner_id: uuid.UUID, token: str) -> bool:
        async with self.sessionmaker() as session:
            released = await ProcessingLockRepository(session).release(
                owner_id=owner_id, token=token
            )
            await session.commit()
            return released


@dataclass(slots=True)
class MemoryProcessingLockStore:
    """Injection-only test double — never selectable by configuration.

    Holds no expiry: TTL is a property of the durable gate, and a test that needed to
    observe expiry would be testing the database's clock, which `tests/test_processing_lock`
    does directly against a row written with a past `expires_at`.
    """

    held: dict[uuid.UUID, str] = field(default_factory=dict)
    #: Every call, in order — what the graph tests assert against.
    calls: list[tuple[str, uuid.UUID, str]] = field(default_factory=list)

    async def acquire(
        self, *, owner_id: uuid.UUID, conversation_id: uuid.UUID | None, token: str
    ) -> None:
        self.calls.append(("acquire", owner_id, token))
        self.held[owner_id] = token

    async def release(self, *, owner_id: uuid.UUID, token: str) -> bool:
        self.calls.append(("release", owner_id, token))
        if self.held.get(owner_id) != token:
            return False
        del self.held[owner_id]
        return True
