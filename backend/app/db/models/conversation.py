"""`conversations` — app-owned conversation records (§4.16, FR-PER-02/04).

`id` doubles as the LangGraph `thread_id` (same UUID, FR-PER-02). `archived` is a
forward-looking column with no feature behind it yet (FR-PER-04). Heavy graph state
lives in the checkpointer, not here (FR-PER-03).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owner_id", "owner_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )  # == LangGraph thread_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'"),  # TBD(OI-21)
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    kb_selections: Mapped[dict | None] = mapped_column(JSONB)
