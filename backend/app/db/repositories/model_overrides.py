"""Model-override repository (T-611, R-83) — three statements, no unit of work.

Like every repository here it never commits; the caller owns the transaction.

The upsert is Core DML rather than a read-modify-write for the reason
:class:`~app.db.repositories.processing_lock.ProcessingLockRepository` gives: two operators
setting the same slot concurrently must compose into one row in either finishing order, and
a `SELECT` followed by an `INSERT`/`UPDATE` races itself. Last writer wins, which is the
right semantics for a knob — there is nothing to serialise on.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.model_override import ModelOverride
from app.db.repositories.base import BaseRepository


class ModelOverrideRepository(BaseRepository[ModelOverride]):
    model = ModelOverride

    async def list_all(self) -> Sequence[ModelOverride]:
        """Every override, for the one read the resolution path makes per turn.

        Deliberately unfiltered and unpaginated: the table holds at most one row per slot —
        five today, six once T-608 unblocks embeddings — so fetching the lot is cheaper than
        the round trips a per-slot read would cost, and it gives the caller a *consistent*
        selection rather than five reads that a concurrent write could interleave.
        """
        return (await self.session.scalars(select(ModelOverride))).all()

    async def set(self, *, slot: str, model_id: str, updated_by: str | None) -> None:
        """Publish ``model_id`` for ``slot``. Last writer wins."""
        stmt = pg_insert(ModelOverride).values(slot=slot, model_id=model_id, updated_by=updated_by)
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[ModelOverride.slot],
                set_={
                    "model_id": stmt.excluded.model_id,
                    "updated_by": stmt.excluded.updated_by,
                },
            )
        )

    async def clear(self, *, slot: str) -> bool:
        """Drop ``slot``'s override, returning whether one existed.

        The boolean is the difference between "reverted to the configured default" and
        "there was nothing to revert" — two outcomes the CLI has to report differently or
        an operator cannot tell a successful clear from a mistyped slot.
        """
        result = await self.session.execute(delete(ModelOverride).where(ModelOverride.slot == slot))
        return bool(result.rowcount)
