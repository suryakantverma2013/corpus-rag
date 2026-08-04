"""Message repository (T-102).

Authoritative for GUI display (OI-23). `evaluation` is populated post-hoc by the
DeepEval job (FR-EVL-01); `feedback` is set from the action bar (FR-MSG-08).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select

from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
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

    async def preceding_user_message(
        self, conversation_id: uuid.UUID, *, message_id: uuid.UUID
    ) -> Message | None:
        """The user turn an answer replies to (T-404, T-309).

        `messages` carries no question↔answer foreign key — §4.16 models a transcript, not a
        pair — so the link is `seq` adjacency: the nearest `USER` row before this one. That is
        exactly what a Regenerate needs (the question to re-run) and what the FR-EVL-01 judge
        needs (DeepEval's `input`), which is why both read it here rather than each walking the
        transcript in their own way.

        Same scalar-subquery bound as :meth:`list_before`, and for the same reason: a
        `message_id` from another conversation yields `None` rather than that conversation's
        last question, because the subquery returns NULL and `seq < NULL` is false for every
        row. `LIMIT 1` on the index, so this never loads the transcript to find one row.
        """
        cutoff = (
            select(Message.seq)
            .where(Message.id == message_id, Message.conversation_id == conversation_id)
            .scalar_subquery()
        )
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.seq < cutoff,
                Message.role == MessageRole.USER,
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def latest_ai_message(self, conversation_id: uuid.UUID) -> Message | None:
        """The newest AI answer in this conversation, or ``None`` (T-404).

        Backs FR-MSG-08's latest-answer-only rule. Regenerating mid-transcript would rewrite an
        answer that every later turn was generated *from*, and nothing in the spec says what
        happens to those — so the route compares this row's id with the target's and refuses
        anything else.
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role == MessageRole.AI)
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def count_before(self, conversation_id: uuid.UUID, *, message_id: uuid.UUID) -> int:
        """How many messages precede ``message_id`` — a row's rank in its conversation (T-404).

        A regenerate must re-run the *original* turn's index, not the transcript's current
        length: `turn_index` correlates the logs of one exchange, and
        :meth:`count_by_conversation` would now return a larger number for the same turn, so a
        regenerated turn would appear in telemetry as a turn that never happened.

        Bounded by the same scalar subquery as :meth:`list_before`, so a foreign id counts zero
        rather than the whole of another conversation.
        """
        cutoff = (
            select(Message.seq)
            .where(Message.id == message_id, Message.conversation_id == conversation_id)
            .scalar_subquery()
        )
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id, Message.seq < cutoff)
        )
        return int(await self.session.scalar(stmt) or 0)

    async def count_by_conversation(self, conversation_id: uuid.UUID) -> int:
        """How many messages this conversation holds (T-402).

        The send route derives `RAGState.turn_index` from it — monotone per thread, which is
        all FR-ORC-03 and the resume diagnostics ask of it. Counted in the database rather
        than by loading the transcript: this runs on every send, and the one thing the caller
        needs is a number.
        """
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        return int(await self.session.scalar(stmt) or 0)

    async def delete_by_conversation(self, conversation_id: uuid.UUID) -> int:
        """Remove every message of a conversation, returning the count (T-401).

        A bulk `DELETE` rather than loading and deleting per row: FR-SBR-07 deletes the whole
        chat, and there is no per-message hook to run. Nothing holds a foreign key to
        `messages`, so this needs no ordering of its own — but the *conversation* row does,
        which is why the caller deletes this first.
        """
        result = await self.session.execute(
            delete(Message).where(Message.conversation_id == conversation_id)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def get_owned(self, message_id: uuid.UUID, *, owner_id: uuid.UUID) -> Message | None:
        """One message, or ``None`` if it does not exist **or sits in someone else's chat** (T-403).

        `messages` carries no `owner_id` — ownership is the conversation's (§4.16) — so the
        predicate is a join, reaching through one table exactly as
        `KnowledgeJobRepository.get_scoped` reaches through `documents`.

        Owner-scoped **in the query**, never by loading the row and comparing in Python (the
        convention FR-RET-04 / NFR-SEC-06 set and `ConversationRepository.get_owned` follows).
        Here it also keeps the two cases indistinguishable, which is R-54: a foreign id is
        `404` and never `403` (NFR-SEC-02), for an administrator too, and there is no widening.

        Deliberately **not** filtered on `role`: this is an ownership lookup and T-404's
        Regenerate needs the identical one. Which roles a route acts on is that route's rule.
        """
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == message_id, Conversation.owner_id == owner_id)
        )
        return (await self.session.scalars(stmt)).first()

    async def set_feedback(self, message: Message, feedback: Feedback | None) -> Message:
        message.feedback = feedback
        await self.session.flush()
        return message

    async def set_evaluation(self, message: Message, evaluation: dict) -> Message:
        message.evaluation = evaluation
        await self.session.flush()
        return message
