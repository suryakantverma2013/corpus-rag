"""Conversation repository (T-102). `id` doubles as the LangGraph thread_id."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.conversation import Conversation
from app.db.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    async def list_by_owner(self, owner_id: uuid.UUID) -> list[Conversation]:
        """Most-recently-updated first (sidebar order, FR-SBR-03)."""
        stmt = (
            select(Conversation)
            .where(Conversation.owner_id == owner_id)
            .order_by(Conversation.updated_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def rename(self, conversation: Conversation, title: str) -> Conversation:
        conversation.title = title
        await self.session.flush()
        return conversation
