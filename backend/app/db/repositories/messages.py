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
        """Oldest first, ordered by `seq` — never by `created_at` (T-108).

        A turn writes the user message and the answer in one transaction, so they share a
        `created_at` (`now()` is the transaction timestamp) and any tiebreak on the random
        UUID `id` is a coin flip on showing the answer above the question.
        """
        stmt = (
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_tail(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """The last ``limit`` messages, returned oldest first (T-304, R-45(6)).

        `ORDER BY seq DESC LIMIT n` reversed in Python, rather than loading the conversation
        and slicing: the router asks this on every turn, and the caller that needs a bounded
        tail is the one that must not pay for an unbounded read. `ix_messages_conversation_id_seq`
        serves the ordering, so this is an index scan of `limit` rows either way.
        """
        if limit <= 0:
            return []
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        return list(reversed(list((await self.session.scalars(stmt)).all())))

    async def set_feedback(self, message: Message, feedback: Feedback | None) -> Message:
        message.feedback = feedback
        await self.session.flush()
        return message

    async def set_evaluation(self, message: Message, evaluation: dict) -> Message:
        message.evaluation = evaluation
        await self.session.flush()
        return message
