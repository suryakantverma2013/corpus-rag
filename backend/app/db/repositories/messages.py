"""Message repository (T-102).

Authoritative for GUI display (OI-23). `evaluation` is populated post-hoc by the
DeepEval job (FR-EVL-01); `feedback` is set from the action bar (FR-MSG-08).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.enums import Feedback
from app.db.models.message import Message
from app.db.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def list_by_conversation(self, conversation_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def set_feedback(self, message: Message, feedback: Feedback | None) -> Message:
        message.feedback = feedback
        await self.session.flush()
        return message

    async def set_evaluation(self, message: Message, evaluation: dict) -> Message:
        message.evaluation = evaluation
        await self.session.flush()
        return message
