"""Auth orchestration (T-103) — the transaction-owning layer above the routes.

``login`` exchanges credentials with Keycloak, validates the returned access
token, and upserts the local ``users`` row keyed to the Keycloak ``sub`` (R-28),
committing the unit of work (repositories only flush). ``change_password``
verifies the current password via ROPC, then resets it through the Admin API.
``refresh``/``logout`` wrap the token-endpoint calls so their audit events live
here alongside login. Keycloak error subclasses propagate to the routes, which
map them to HTTP status + the spec's copy strings.

Every mutating path emits an ``AuditEventType.AUTH`` record before committing, so
the audit row lands atomically with the action (T-107, NFR-SEC-08).
"""

from __future__ import annotations

import uuid
from typing import Any

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keycloak_client import (
    InvalidCredentialsError,
    KeycloakClient,
    TooManyAttemptsError,
)
from app.auth.principal import Principal
from app.auth.tokens import validate_access_token
from app.db.repositories.users import UserRepository
from app.services import audit


async def login(
    *, email: str, password: str, kc: KeycloakClient, session: AsyncSession
) -> dict[str, Any]:
    try:
        tokens = await kc.password_grant(email, password)
    except (InvalidCredentialsError, TooManyAttemptsError) as exc:
        # Audit the failed attempt (no principal → actor_id null), then re-raise.
        # KeycloakUnavailableError is intentionally *not* caught here: it's an
        # infra failure, not an authentication event.
        await audit.record_auth(
            session,
            action="login_failed",
            actor_id=None,
            details={"email": email, "reason": type(exc).__name__},
        )
        await session.commit()
        raise
    principal = await validate_access_token(tokens["access_token"])
    await UserRepository(session).upsert_from_claims(
        sub=principal.sub, email=principal.email, display_name=principal.display_name
    )
    await audit.record_auth(
        session, action="login", actor_id=principal.sub, details={"email": principal.email}
    )
    await session.commit()
    return tokens


async def refresh(
    *, refresh_token: str, kc: KeycloakClient, session: AsyncSession
) -> dict[str, Any]:
    tokens = await kc.refresh(refresh_token)
    await audit.record_auth(
        session, action="refresh", actor_id=_subject_from_token(tokens.get("access_token"))
    )
    await session.commit()
    return tokens


async def logout(*, refresh_token: str, kc: KeycloakClient, session: AsyncSession) -> None:
    await kc.logout(refresh_token)
    await audit.record_auth(session, action="logout", actor_id=_subject_from_token(refresh_token))
    await session.commit()


async def change_password(
    *,
    principal: Principal,
    current_password: str,
    new_password: str,
    kc: KeycloakClient,
    session: AsyncSession,
) -> None:
    # (a) verify the current password (raises InvalidCredentialsError → 401).
    await kc.password_grant(principal.email, current_password)
    # (b) obtain a service-account token, (c) reset via the Admin API.
    admin_token = await kc.service_account_token()
    await kc.admin_reset_password(
        sub=str(principal.sub), new_password=new_password, admin_token=admin_token
    )
    await audit.record_auth(session, action="password_change", actor_id=principal.sub)
    await session.commit()


def _subject_from_token(token: str | None) -> uuid.UUID | None:
    """Best-effort ``sub`` from a Keycloak JWT, for an audit *detail* only.

    Unverified decode — never an authorization decision. Refresh/logout tokens are
    already validated by Keycloak on the call above; here we only want the subject
    to attribute the event. Any malformed/opaque token → ``None`` (event still records).
    """
    if not token:
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return uuid.UUID(str(claims["sub"]))
    except jwt.PyJWTError, KeyError, ValueError:
        return None
