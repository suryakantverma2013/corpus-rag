"""`knowledge_bases` — R-25 two-scope model mapped to a first-class entity.

GLOBAL ↔ the user's default KB; CONVERSATION ↔ an implicit per-chat KB. `tenant_id`
is carried for the FR-RET-04 data-layer access filter (# TBD(OI-21)).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.enums import KBVisibility, str_enum


class KnowledgeBase(TimestampMixin, Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'"),  # TBD(OI-21)
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[KBVisibility] = mapped_column(str_enum(KBVisibility), nullable=False)
