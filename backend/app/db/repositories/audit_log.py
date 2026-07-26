"""Audit-log repository (T-107) — the write + read surface for the trail (NFR-SEC-08).

Append-only by convention: only ``record`` writes (there is no update/delete of an
audit row). ``list_events`` backs the admin read endpoint; filters are applied in the
query. Like every repository it flushes but never commits — the caller (an audit
service helper, invoked inside the owning unit of work) commits so the audit row lands
atomically with the action it records.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.db.enums import AuditEventType
from app.db.models.audit_log import AuditLog
from app.db.repositories.base import BaseRepository


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def record(
        self,
        *,
        event_type: AuditEventType,
        actor_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        details: dict | None = None,
    ) -> AuditLog:
        """Append one audit row (flush only — the caller owns the commit)."""
        entry = AuditLog(
            event_type=event_type,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        return await self.add(entry)

    async def list_events(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        event_type: AuditEventType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AuditLog]:
        """Newest-first audit rows, optionally filtered by actor and/or event type.

        The ``id`` tiebreak keeps ordering deterministic when rows share a
        ``created_at`` (``now()`` is the transaction timestamp — see T-108).
        """
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if event_type is not None:
            stmt = stmt.where(AuditLog.event_type == event_type)
        stmt = stmt.limit(limit).offset(offset)
        return (await self.session.scalars(stmt)).all()
