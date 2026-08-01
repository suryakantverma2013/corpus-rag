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

    async def list_contents(self, conversation_id: uuid.UUID) -> list[str]:
        """Just the text of every message in the conversation, oldest first (T-310).

        Backs the NFR-CAP-01 meter, which needs the content and nothing else — no ORM
        instances, no `citations`/`evaluation` JSONB, which for a long chat is the bulk of
        the row and is read on every send otherwise.

        It returns the **text** rather than a SQL-side `SUM(LENGTH(content))` on purpose:
        the characters-per-token rule lives in exactly one place (`app.tokens`), and pushing
        half of it into a query would put a second copy in a dialect where nobody would think
        to look when the number stopped matching the GUI's.
        """
        stmt = (
            select(Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_before(
        self, conversation_id: uuid.UUID, *, message_id: uuid.UUID
    ) -> list[Message]:
        """Everything strictly before ``message_id``, oldest first (T-307, R-48(7)).

        The generation prompt needs the transcript *without* the row it is answering: T-402
        writes the user's message before starting the graph, and `compose_messages` adds the
        query again as the last message — so an unbounded read would put the question in the
        prompt twice, once before the fenced context and once after it.

        Bounded by a scalar subquery on `seq` rather than by loading and slicing in Python,
        so the exclusion holds even if a concurrent write lands mid-read. A `message_id` that
        is not in this conversation yields the empty list, not the whole transcript: the
        subquery returns NULL and `seq < NULL` is false for every row. That is the safe
        direction — an absent history costs quality, a mis-scoped one leaks another
        conversation's turns into this prompt.
        """
        cutoff = (
            select(Message.seq)
            .where(Message.id == message_id, Message.conversation_id == conversation_id)
            .scalar_subquery()
        )
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.seq < cutoff)
            .order_by(Message.seq)
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
