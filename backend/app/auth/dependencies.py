"""FastAPI auth dependencies — the request-time authorization seams (T-103).

Layered so each seam is independently overridable in tests:
``bearer_scheme`` → ``get_principal`` (token-only, no DB; validate claims) →
``get_current_user`` (loads the local ``users`` row, enforces ``is_active``) →
``require_admin`` (claim-based role check, NFR-SEC-01). This module also
establishes the codebase's ``Annotated[..., Depends(...)]`` convention.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.keycloak_client import KeycloakClient
from app.auth.principal import Principal
from app.auth.tokens import TokenValidationError, validate_access_token
from app.config import Settings, get_settings
from app.db.models.users import User
from app.db.repositories.users import UserRepository
from app.db.session import get_session, get_stream_sessionmaker

#: `auto_error=False` so `get_principal` raises the 401 itself, with `WWW-Authenticate` and the
#: FR-AUT copy. The consequence for the schema is that FastAPI never sees that 401 — which is
#: why `app/openapi.py` derives 401/403 from the `security` block this scheme emits (T-405).
bearer_scheme = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description="Realm-signed RS256 access token from `POST /api/v1/auth/login` (R-28).",
)

_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal:
    """Validate the bearer token and return the :class:`Principal`. No DB access.

    Also stashes the principal on ``request.state`` so the rate limiter's
    per-user key function (``principal_or_ip_key``, T-105) can read it without
    re-validating the token.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", _UNAUTH_HEADERS)
    try:
        principal = await validate_access_token(credentials.credentials)
    except TokenValidationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", _UNAUTH_HEADERS
        ) from exc
    request.state.principal = principal
    return principal


async def get_current_user(
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Load the local user row and enforce ``is_active`` (immediate deactivation)."""
    user = await UserRepository(session).get(principal.sub)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found", _UNAUTH_HEADERS)
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User account is disabled")
    return user


async def require_admin(
    principal: Annotated[Principal, Depends(get_principal)],
    _user: Annotated[User, Depends(get_current_user)],
) -> Principal:
    """Administrator-only gate: role from claims (NFR-SEC-01) + active-account check."""
    if not principal.is_administrator:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    return principal


async def get_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    _principal: Annotated[Principal, Depends(get_principal)],
) -> str:
    """The caller's **raw** access token, for the one thing that genuinely needs it (T-214).

    Keycloak's broker endpoint returns *this user's* provider token and authenticates with
    *this user's* token — the service account cannot stand in, because the token being fetched
    is not the service account's (see `KeycloakClient.broker_token`). So FR-AUT-11 import is
    the only caller; nothing else in the API should reach for this.

    It depends on `get_principal` rather than reading the header alone, so the string handed
    out has already been validated as a live realm-signed token. Without that this would be a
    way to forward an arbitrary header value to Keycloak.
    """
    # `get_principal` has already rejected the None/empty case, so this is narrowing for the
    # type checker rather than a second check.
    if credentials is None:  # pragma: no cover - unreachable via get_principal
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", _UNAUTH_HEADERS)
    return credentials.credentials


def get_keycloak_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> KeycloakClient:
    return KeycloakClient(settings.keycloak)


CurrentAccessToken = Annotated[str, Depends(get_access_token)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentUser = Annotated[User, Depends(get_current_user)]
RequireAdmin = Annotated[Principal, Depends(require_admin)]
Keycloak = Annotated[KeycloakClient, Depends(get_keycloak_client)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
# For handlers that outlive their request (SSE streams, T-210) — see
# `get_stream_sessionmaker`. Never use this where `DbSession` will do.
StreamSessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_stream_sessionmaker)]
