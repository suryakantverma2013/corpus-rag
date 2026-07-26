"""Generic async repository base (T-102).

Repositories own all queries (backend-dev convention) and operate on an injected
`AsyncSession`. They `flush` (so server defaults / PKs populate) but never
`commit` — the caller owns the unit of work.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base


class BaseRepository[ModelT: Base]:
    """CRUD common to `id`-keyed models. Subclasses add entity-specific queries."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id_: uuid.UUID) -> ModelT | None:
        return await self.session.get(self.model, id_)

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        stmt = select(self.model).limit(limit).offset(offset)
        return (await self.session.scalars(stmt)).all()

    async def add(self, instance: ModelT) -> ModelT:
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def delete(self, instance: ModelT) -> None:
        await self.session.delete(instance)
        await self.session.flush()
