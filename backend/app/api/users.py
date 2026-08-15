"""``/users`` routes — admin-only user management (T-104, R-25 surface).

Every route is gated by ``require_admin`` (NFR-SEC-01, FR-USR-07); the actor
principal is injected for the self-protection guard. Thin handlers: orchestration
lives in ``app.auth.users_service`` / ``KeycloakClient``. This layer maps Keycloak
+ service error subclasses to HTTP status codes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.errors import MISCONFIGURED, RATE_LIMITED, UPSTREAM_DOWN, error_responses
from app.auth import users_service
from app.auth.dependencies import DbSession, Keycloak, RequireAdmin
from app.auth.keycloak_client import (
    KeycloakForbiddenError,
    KeycloakRejectedError,
    KeycloakUnavailableError,
    TooManyAttemptsError,
    UserConflictError,
    UserNotFoundError,
)
from app.auth.schemas import CreateUserRequest, UpdateUserRequest, UserResponse
from app.auth.users_service import SelfMutationError

router = APIRouter(prefix="/users", tags=["users"])

#: Every route here talks to Keycloak's Admin API, so all four share the same failure set
#: (T-110's narrowing: `503` means unreachable, `500` means our own credentials or request).
#: Declared centrally as of T-405 — before it, these routes carried no `responses=` at all.
_KEYCLOAK_FAILURES = {**RATE_LIMITED, **MISCONFIGURED, **UPSTREAM_DOWN}

_CONFLICT = "A user with that email already exists."
_NOT_FOUND = "User not found."
_SELF = "You cannot perform this action on your own account."
_RATE_LIMITED = "Too many attempts — try again later."
# 503: transient by construction (T-110) — Keycloak unreachable, timed out, or 5xx. The copy
# says "unavailable" and means it, so nothing that needs a configuration change may use it.
_UPSTREAM = "Authentication service unavailable."
# 500: the server's own Keycloak credentials are wrong or under-provisioned, or we sent a
# request Keycloak rejected. The caller is an authenticated administrator who did nothing
# wrong and can do nothing about it, so the copy points at the one place that can — the logs,
# where `keycloak.admin_call_failed` carries the method, path and status. # TBD(§8.4)
_MISCONFIGURED = (
    "User administration is not configured correctly on the server. "
    "Check the server logs for details."
)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **error_responses((409, "A user with that email already exists.")),
        **_KEYCLOAK_FAILURES,
    },
    summary="Create a user",
)
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
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.get(
    "",
    response_model=list[UserResponse],
    responses=_KEYCLOAK_FAILURES,
    summary="List users",
)
async def list_users(
    _admin: RequireAdmin,
    kc: Keycloak,
    first: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    search: str | None = Query(default=None),
) -> list[UserResponse]:
    try:
        return await users_service.list_users(kc=kc, first=first, limit=limit, search=search)
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    responses={
        **error_responses(
            (404, "User not found."),
            # R-77(2): the declared envelope is the contract the generated client derives
            # from, so it has to be the statuses this route can actually answer. It answered
            # `409` for the self-mutation guard while declaring `403` for it and declaring a
            # duplicate-email `409` it cannot raise — `update_user` has no `UserConflictError`
            # path, that is `create_user`'s. The `403`s this route really returns (no
            # administrator role, disabled account) are injected from its `security` block by
            # `app/openapi.py`, so they were never this declaration's to make.
            (409, "You cannot perform this action on your own account."),
        ),
        **_KEYCLOAK_FAILURES,
    },
    summary="Update a user",
)
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
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **error_responses(
            (404, "User not found."),
            # R-77(2), as above: the guard answers `409` and this declared `403` and no `409`
            # at all, so a generated client had no type for the one refusal this route makes
            # on its own account.
            (409, "You cannot perform this action on your own account."),
        ),
        **_KEYCLOAK_FAILURES,
    },
    summary="Delete a user",
)
async def delete_user(
    user_id: uuid.UUID, admin: RequireAdmin, kc: Keycloak, session: DbSession
) -> None:
    try:
        await users_service.delete_user(sub=user_id, actor=admin, kc=kc, session=session)
    except SelfMutationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _SELF) from exc
    except UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
