"""Knowledge-base repository (T-102).

Supports the R-25 two-scope model: a per-owner GLOBAL default KB and implicit
per-conversation KBs. `get_or_create_default` gives each user one GLOBAL KB.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import KBVisibility
from app.db.models.knowledge_base import KnowledgeBase
from app.db.repositories.base import BaseRepository


class KnowledgeBaseRepository(BaseRepository[KnowledgeBase]):
    model = KnowledgeBase

    async def list_by_owner(self, owner_id: uuid.UUID) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(KnowledgeBase.owner_id == owner_id)
        return list((await self.session.scalars(stmt)).all())

    async def get_default(self, owner_id: uuid.UUID) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.owner_id == owner_id,
            KnowledgeBase.visibility == KBVisibility.GLOBAL,
        )
        return (await self.session.scalars(stmt)).first()

    async def get_or_create_default(
        self,
        owner_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
        name: str = "My Knowledge Base",
    ) -> KnowledgeBase:
        kb = await self.get_default(owner_id)
        if kb is None:
            kb = KnowledgeBase(
                owner_id=owner_id,
                tenant_id=tenant_id,
                name=name,
                visibility=KBVisibility.GLOBAL,
            )
            self.session.add(kb)
            await self.session.flush()
        return kb

    async def get_for_conversation(self, conversation_id: uuid.UUID) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(KnowledgeBase.conversation_id == conversation_id)
        return (await self.session.scalars(stmt)).first()

    async def get_or_create_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        owner_id: uuid.UUID,
        tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
        name: str = "Chat attachments",
    ) -> KnowledgeBase:
        """The implicit per-conversation KB backing THIS-CHAT attachments (R-25/R-33).

        Materialised on first attachment rather than with the conversation, so chats that
        never receive an upload cost nothing. `knowledge_bases.conversation_id` is unique,
        so a concurrent second upload loses the insert race with an `IntegrityError`
        rather than creating a second attachment KB — the caller re-reads.
        """
        kb = await self.get_for_conversation(conversation_id)
        if kb is None:
            kb = KnowledgeBase(
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                name=name,
                visibility=KBVisibility.CONVERSATION,
            )
            self.session.add(kb)
            await self.session.flush()
        return kb
