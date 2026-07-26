"""`audit_log` — append-only security/lifecycle trail (NFR-SEC-08, designed here).

Distinct from operational telemetry. Rows are written for auth events, user/role
changes, document upload/delete, and permission changes; `actor_id` is nullable for
pre-auth events. No source DDL exists — this table is new for T-101. Append-only by
convention (service writes only; retention # TBD(§8.4)).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin
from app.db.enums import AuditEventType, str_enum


class AuditLog(CreatedAtMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_actor_id", "actor_id"),
        Index("ix_audit_log_event_type_created_at", "event_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    event_type: Mapped[AuditEventType] = mapped_column(str_enum(AuditEventType), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict | None] = mapped_column(JSONB)
