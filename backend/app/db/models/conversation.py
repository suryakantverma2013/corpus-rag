"""`conversations` — app-owned conversation records (§4.16, FR-PER-02/04).

`id` doubles as the LangGraph `thread_id` (same UUID, FR-PER-02). `archived` is a
forward-looking column with no feature behind it yet (FR-PER-04). Heavy graph state
lives in the checkpointer, not here (FR-PER-03).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owner_id", "owner_id"),)

    # Overrides `TimestampMixin.updated_at` to use `clock_timestamp()` (T-108).
    #
    # This column *is* the FR-SBR-03 sidebar order, so it has to advance whenever the row
    # is touched. `now()` — the mixin's default, and correct for every other model — is
    # the **transaction** timestamp: it is frozen for the whole transaction, so renaming a
    # conversation gives it an `updated_at` equal to that of any sibling written in the
    # same transaction, and "most recently updated first" then falls through to an
    # arbitrary tiebreak. `clock_timestamp()` is the real wall clock and advances mid
    # transaction, which makes the ordering genuinely correct rather than merely
    # deterministic. Kept local to this model: elsewhere `now()`'s "one logical time per
    # transaction" is the more useful semantic.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )  # == LangGraph thread_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'"),  # single-org (OI-21)
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    kb_selections: Mapped[dict | None] = mapped_column(JSONB)
