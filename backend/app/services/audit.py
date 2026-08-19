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
_TARGET_CONVERSATION = "conversation"

# Actions that are *not* removals. Everything else falls through to DOCUMENT_DELETE —
# see `record_document_event`.
_DOCUMENT_EVENTS = {
    "upload": AuditEventType.DOCUMENT_UPLOAD,
    "replace": AuditEventType.DOCUMENT_REPLACE,
    # T-608's re-embed (R-84(5)). `DOCUMENT_REPLACE` rather than a new member: it repoints
    # `storage_uri` and bumps the version exactly as a replace does, and adding an
    # `AuditEventType` would be the CHECK-constraint surgery R-43(7) declined for a
    # distinction `details.action` already carries. Without this line it would default to
    # `DOCUMENT_DELETE` — the very miss the docstring below warns about.
    "rebuild": AuditEventType.DOCUMENT_REPLACE,
}


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


async def record_authorization_denied(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    conversation_id: uuid.UUID,
    turn_index: int | None = None,
) -> AuditLog:
    """FR-ORC-02 — a caller was refused a conversation that is not theirs (T-302, R-43).

    Filed under ``AUTH`` with an ``action`` discriminator rather than a new
    :class:`AuditEventType` member, which is this module's existing convention: ``AUTH``
    already multiplexes login / login_failed / logout / refresh / password_change, and
    ``AuditEventType`` is a six-member *category* enum whose NFR-SEC-08 category for this is
    "auth". A new member would cost a hand-written CHECK-constraint migration
    (`native_enum=False`) to buy a filter ``details`` already provides.

    Written only for the **ownership** failure. A conversation that no longer exists is a
    resumed run whose chat was deleted (R-42(11)/T-401) — ordinary, and not a security
    event; the graph logs that case instead.

    Not idempotent on resume: ``audit_log`` has no idempotency key, so a `govern` that
    re-executes after a crash appends a second row. ``turn_index`` is carried so duplicates
    are recognisable, which is the honest fix — a genuine second attempt and a replayed one
    are not distinguishable from inside the node.
    """
    return await AuditLogRepository(session).record(
        event_type=AuditEventType.AUTH,
        actor_id=actor_id,
        target_type=_TARGET_CONVERSATION,
        target_id=str(conversation_id),
        details={"action": "chat_access_denied", "turn_index": turn_index},
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
    """Document lifecycle audit event (upload, replace, delete).

    `DOCUMENT_DELETE` is the deliberate default rather than a mapping miss: it also covers
    T-207's ``"malware_purge"``, which removes the stored original and is fairly filed as a
    deletion. Any *new* action that is not a removal needs its own entry here — until
    T-209 added ``"replace"`` this was a ternary, so a replace was recorded in a security
    artefact as a deletion.
    """
    event_type = _DOCUMENT_EVENTS.get(action, AuditEventType.DOCUMENT_DELETE)
    return await AuditLogRepository(session).record(
        event_type=event_type,
        actor_id=actor_id,
        target_type=_TARGET_DOCUMENT,
        target_id=str(document_id),
        details={"action": action},
    )
