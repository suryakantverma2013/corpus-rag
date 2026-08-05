"""``/audit`` route — admin-only read of the audit trail (T-107, NFR-SEC-08).

Read-only surface over the append-only trail; writes happen via the hooks in the
auth/user services (``app.services.audit``). Admin-gated (NFR-SEC-01) — the audit
trail is a security artefact. Filters (actor, event type) and paging apply in the
query. Retention/export policy is # TBD(§8.4).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.auth.dependencies import DbSession, RequireAdmin
from app.db.enums import AuditEventType
from app.db.repositories.audit_log import AuditLogRepository

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    event_type: AuditEventType
    target_type: str | None
    target_id: str | None
    #: Deliberately left open (T-405). The payload differs per `event_type` — R-43(7)'s
    #: `details.action` for a chat denial, R-39's job ids elsewhere — and no GUI surface reads
    #: it, so a model here would freeze a shape nothing consumes and every new audit event
    #: would have to widen it.
    details: dict | None
    created_at: datetime.datetime


@router.get(
    "",
    response_model=list[AuditLogResponse],
    summary="Read the audit trail",
)
async def list_audit_events(
    _admin: RequireAdmin,
    session: DbSession,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    event_type: Annotated[AuditEventType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogResponse]:
    rows = await AuditLogRepository(session).list_events(
        actor_id=actor_id, event_type=event_type, limit=limit, offset=offset
    )
    return [AuditLogResponse.model_validate(row, from_attributes=True) for row in rows]
