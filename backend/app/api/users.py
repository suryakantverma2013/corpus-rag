"""``/users`` routes — admin-only user management (T-104, R-25 surface).

Every route is gated by ``require_admin`` (NFR-SEC-01, FR-USR-07); the actor
principal is injected for the self-protection guard. Thin handlers: orchestration
lives in ``app.auth.users_service`` / ``KeycloakClient``. This layer maps Keycloak
+ service error subclasses to HTTP status codes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.auth import users_service
from app.auth.dependencies import DbSession, Keycloak, RequireAdmin
from app.auth.keycloak_client import (
    KeycloakUnavailableError,
    TooManyAttemptsError,
    UserConflictError,
    UserNotFoundError,
)
from app.auth.schemas import CreateUserRequest, UpdateUserRequest, UserResponse
from app.auth.users_service import SelfMutationError

router = APIRouter(prefix="/users", tags=["users"])

_CONFLICT = "A user with that email already exists."
_NOT_FOUND = "User not found."
_SELF = "You cannot perform this action on your own account."
_RATE_LIMITED = "Too many attempts — try again later."
_UPSTREAM = "Authentication service unavailable."


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest, admin: RequireAdmin, kc: Keycloak, session: DbSession
) -> UserResponse:
    try:
        return await users_service.create_user(
            email=body.email,
            display_name=body.display_name,
            password=body.password,
            role=body.role,
            actor=admin,
            kc=kc,
            session=session,
        )
    except UserConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _CONFLICT) from exc
    except TooManyAttemptsError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMITED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.get("", response_model=list[UserResponse])
async def list_users(
    _admin: RequireAdmin,
    kc: Keycloak,
    first: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    search: str | None = Query(default=None),
) -> list[UserResponse]:
    try:
        return await users_service.list_users(kc=kc, first=first, limit=limit, search=search)
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    admin: RequireAdmin,
    kc: Keycloak,
    session: DbSession,
) -> UserResponse:
    try:
        return await users_service.update_user(
            sub=user_id, patch=body, actor=admin, kc=kc, session=session
        )
    except SelfMutationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _SELF) from exc
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except TooManyAttemptsError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMITED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, admin: RequireAdmin, kc: Keycloak, session: DbSession
) -> None:
    try:
        await users_service.delete_user(sub=user_id, actor=admin, kc=kc, session=session)
    except SelfMutationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _SELF) from exc
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
