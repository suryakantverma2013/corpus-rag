"""`messages` — authoritative store for GUI display (§4.16, FR-MSG-06, OI-23).

Message model per FR-MSG-06. `content` is the answer text; `citations` holds the
interleaved citation segments `{isCite, doc, page, quote, chunkId, score}`;
`evaluation` holds the post-hoc DeepEval scores `{relevancy, faithfulness,
precision, recall}` (nullable until the eval job runs, FR-EVL-01). `feedback` is the
spec-added thumbs column (FR-MSG-06/08). Per-message token/latency columns roll up
per chat for OI-16 accounting.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin
from app.db.enums import Feedback, MessageRole, str_enum


class Message(CreatedAtMixin, Base):
    __tablename__ = "messages"
    # Compound index serves both the per-conversation lookup and its `seq` ordering; it
    # supersedes the plain `conversation_id` index, whose job it does as a prefix.
    __table_args__ = (Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    # Display order within a conversation (T-108). **Not `created_at`.**
    #
    # `created_at` defaults to `now()`, which is the *transaction* timestamp: a chat turn
    # writes the user message and the AI answer in one transaction, so both carry the
    # identical value and any `(created_at, id)` ordering falls through to the random UUID
    # tiebreak — a coin flip on rendering the answer above the question (FR-MSG-06,
    # §4.16). A database IDENTITY sequence is monotonic by construction, so it cannot tie
    # even for rows inserted in the same statement, and it needs no lock, no `MAX(seq)+1`
    # read and no retry on a unique violation.
    #
    # Globally allocated rather than per-conversation: only the *relative* order within a
    # conversation is meaningful, nothing renders "message 3 of 7", and gaps are free.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[dict | None] = mapped_column(JSONB)
    evaluation: Mapped[dict | None] = mapped_column(JSONB)
    feedback: Mapped[Feedback | None] = mapped_column(str_enum(Feedback))
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
