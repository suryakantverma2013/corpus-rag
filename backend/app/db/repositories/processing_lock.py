"""Processing-lock repository (T-302, R-43) — the three statements the R-24 gate needs.

Like every repository here it never commits; the caller owns the unit of work. Unlike
most, its three methods are Core DML rather than ORM unit-of-work operations, because
each one has to be a single statement to be correct: the upsert must be atomic against a
concurrent acquire, and the release must match on the token *in the same statement* that
deletes, or the compare and the delete race each other.

Time comes from **the database**, never the application process: with more than one API
host, two clocks that disagree would make one host's lock outlive or predecease the other's
view of it. `clock_timestamp()` rather than `now()` for the same reason it was chosen for
`conversations.updated_at` in T-108 — `now()` is the transaction timestamp, and the reader
here runs inside a request transaction that may have opened some time earlier.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.base import BaseRepository


class ProcessingLockRepository(BaseRepository[ProcessingLock]):
    model = ProcessingLock

    async def acquire(
        self,
        *,
        owner_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        token: str,
        ttl_seconds: float,
    ) -> None:
        """Publish the gate for ``owner_id``. Last writer wins — deliberately no ``NX``.

        The lock is a published flag, not a mutual-exclusion primitive: nothing serialises
        on it, so a failed acquire would leave the `lock` node choosing between blocking,
        erroring and proceeding unlocked — all three worse than simply overwriting. Two
        overlapping turns then hold the gate until the later one finishes, and the token
        makes the earlier one's release a no-op.
        """
        expires_at = func.clock_timestamp() + timedelta(seconds=ttl_seconds)
        stmt = pg_insert(ProcessingLock).values(
            owner_id=owner_id,
            conversation_id=conversation_id,
            token=token,
            acquired_at=func.clock_timestamp(),
            expires_at=expires_at,
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[ProcessingLock.owner_id],
                set_={
                    "conversation_id": stmt.excluded.conversation_id,
                    "token": stmt.excluded.token,
                    "acquired_at": stmt.excluded.acquired_at,
                    "expires_at": stmt.excluded.expires_at,
                },
            )
        )

    async def release(self, *, owner_id: uuid.UUID, token: str) -> bool:
        """Delete the gate only if ``token`` still holds it. Returns whether a row went.

        A ``False`` is normal, not an error: it means this turn's lock had already been
        overwritten by a later turn or had expired and been re-taken. Releasing by
        ``owner_id`` alone would in that case free a *live* turn's gate.
        """
        result = await self.session.execute(
            delete(ProcessingLock).where(
                ProcessingLock.owner_id == owner_id, ProcessingLock.token == token
            )
        )
        return bool(result.rowcount)

    async def active_for(self, owner_id: uuid.UUID) -> ProcessingLock | None:
        """The caller's unexpired lock, or ``None``.

        Returns the row rather than its ``conversation_id`` so a live lock on a
        conversation-less turn is not indistinguishable from no lock at all.
        """
        stmt = select(ProcessingLock).where(
            ProcessingLock.owner_id == owner_id,
            ProcessingLock.expires_at > func.clock_timestamp(),
        )
        return (await self.session.scalars(stmt)).one_or_none()
