"""Audit-log write helpers (T-107, NFR-SEC-08).

Thin, event-shaped wrappers over :class:`AuditLogRepository.record`. Callers are the
transaction-owning orchestration functions (``app.auth.service`` / ``users_service``);
each helper flushes only, so the audit row commits atomically with the action when the
caller commits. Keep this module free of framework/request concerns — actor and target
identities are passed in.

The trail is append-only and distinct from operational telemetry (that is the Telemetry
Agent / NFR-OBS-01). Retention is # TBD(§8.4) — no purge is implemented.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AuditEventType
from app.db.models.audit_log import AuditLog
from app.db.repositories.audit_log import AuditLogRepository

_TARGET_USER = "user"
_TARGET_DOCUMENT = "document"


async def record_auth(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Authentication event (login / login_failed / logout / refresh / password_change).

    ``actor_id`` is null for pre-auth events (e.g. a failed login).
    """
    return await AuditLogRepository(session).record(
        event_type=AuditEventType.AUTH,
        actor_id=actor_id,
        target_type=_TARGET_USER,
        target_id=str(actor_id) if actor_id is not None else None,
        details={"action": action, **(details or {})},
    )


async def record_user_change(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
    action: str,
    details: dict | None = None,
) -> AuditLog:
    """User lifecycle change (create / update / delete) by an admin actor."""
    return await AuditLogRepository(session).record(
        event_type=AuditEventType.USER_ROLE_CHANGE,
        actor_id=actor_id,
        target_type=_TARGET_USER,
        target_id=str(target_user_id),
        details={"action": action, **(details or {})},
    )


async def record_permission_change(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
    details: dict,
) -> AuditLog:
    """Role/permission grant or revoke on a user (e.g. admin promote/demote)."""
    return await AuditLogRepository(session).record(
        event_type=AuditEventType.PERMISSION_CHANGE,
        actor_id=actor_id,
        target_type=_TARGET_USER,
        target_id=str(target_user_id),
        details=details,
    )


async def record_document_event(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    document_id: uuid.UUID,
    action: str,
) -> AuditLog:
    """Document upload/delete audit event.

    NOTE(T-202): no HTTP surface emits these yet — the documents/ingestion routes are
    Phase 2. This helper is ready so those flows wire it in one call.
    """
    event_type = (
        AuditEventType.DOCUMENT_UPLOAD if action == "upload" else AuditEventType.DOCUMENT_DELETE
    )
    return await AuditLogRepository(session).record(
        event_type=event_type,
        actor_id=actor_id,
        target_type=_TARGET_DOCUMENT,
        target_id=str(document_id),
        details={"action": action},
    )
