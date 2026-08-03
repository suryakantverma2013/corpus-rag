"""Conversation repository (T-102). `id` doubles as the LangGraph thread_id."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.conversation import Conversation
from app.db.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    async def list_by_owner(self, owner_id: uuid.UUID) -> list[Conversation]:
        """Most-recently-updated first (sidebar order, FR-SBR-03).

        `Conversation.updated_at` overrides the shared mixin to use `clock_timestamp()`
        precisely so this ordering is correct rather than merely deterministic — see the
        model. `created_at` breaks a genuine tie (two rows written in the same instant),
        which only says "neither is newer".
        """
        stmt = (
            select(Conversation)
            .where(Conversation.owner_id == owner_id)
            .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_owned(
        self, conversation_id: uuid.UUID, *, owner_id: uuid.UUID
    ) -> Conversation | None:
        """One conversation, or ``None`` if it does not exist **or belongs to someone else**.

        The two cases are deliberately indistinguishable (T-401): every conversation route
        answers a foreign id with `404`, so the response cannot confirm that an id exists —
        NFR-SEC-02, the same rule `KnowledgeJobRepository.get_scoped` follows.

        Owner-scoped **in the query**, never by loading and comparing in Python (FR-RET-04 /
        NFR-SEC-06 is about retrieval, but the convention is the codebase's).

        There is no administrator widening here and that is R-54: nothing in the spec asks for
        admin chat impersonation, and `POST .../messages` would otherwise write a turn into
        someone else's durable transcript (OI-33).
        """
        stmt = select(Conversation).where(
            Conversation.id == conversation_id, Conversation.owner_id == owner_id
        )
        return await self.session.scalar(stmt)

    async def rename(self, conversation: Conversation, title: str) -> Conversation:
        conversation.title = title
        await self.session.flush()
        # `onupdate` fires server-side, so the in-session object still holds the old
        # value; refresh it or a caller that lists straight after renaming sorts on stale
        # data. (`expire_on_commit=False` means nothing else would reload it.)
        await self.session.refresh(conversation, ["updated_at"])
        return conversation
