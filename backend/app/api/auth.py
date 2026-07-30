"""``/auth`` routes (R-25 surface, R-28 Keycloak-backed) — T-103.

Thin handlers: orchestration lives in ``app.auth.service`` / ``KeycloakClient``.
This layer only maps Keycloak error subclasses to HTTP status codes and the
spec's copy strings (FR-AUT-04/06/08/09).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.auth import service
from app.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession, Keycloak
from app.auth.keycloak_client import (
    InvalidCredentialsError,
    KeycloakForbiddenError,
    KeycloakRejectedError,
    KeycloakUnavailableError,
    TooManyAttemptsError,
)
from app.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from app.security.rate_limit import (
    RATE_LIMITED_COPY,
    change_password_limit,
    client_ip_key,
    limiter,
    login_limit,
    principal_or_ip_key,
    refresh_limit,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# FR-AUT-04 copy strings.
_INVALID_LOGIN = "Invalid email or password."
# The throttle copy is shared with the rate limiter (T-105); this alias keeps the
# Keycloak-lock backstop below wording-identical to the slowapi 429.
_RATE_LIMITED = RATE_LIMITED_COPY
_WRONG_CURRENT = "Current password is incorrect."
_UPSTREAM = "Authentication service unavailable."
# T-110: change-password calls the Admin API's `reset-password`, so it can hit the same
# under-provisioned-service-account condition as the /users routes. Same reasoning, same 500.
_MISCONFIGURED = (
    "Password change is not configured correctly on the server. "
    "Check the server logs for details."
)  # TBD(§8.4)


def _token_response(tokens: dict[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type="bearer",
        expires_in=int(tokens.get("expires_in", 0)),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit(login_limit, key_func=client_ip_key)
async def login(
    request: Request, response: Response, body: LoginRequest, kc: Keycloak, session: DbSession
) -> TokenResponse:
    try:
        tokens = await service.login(
            email=body.email, password=body.password, kc=kc, session=session
        )
    except TooManyAttemptsError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMITED) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_LOGIN) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
    return _token_response(tokens)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(refresh_limit, key_func=client_ip_key)
async def refresh(
    request: Request, response: Response, body: RefreshRequest, kc: Keycloak, session: DbSession
) -> TokenResponse:
    try:
        tokens = await service.refresh(refresh_token=body.refresh_token, kc=kc, session=session)
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session.") from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
    return _token_response(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, kc: Keycloak, session: DbSession) -> None:
    try:
        await service.logout(refresh_token=body.refresh_token, kc=kc, session=session)
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal, user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=sorted(role.value for role in principal.roles),
        is_active=user.is_active,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(change_password_limit, key_func=principal_or_ip_key)
async def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    principal: CurrentPrincipal,
    _user: CurrentUser,
    kc: Keycloak,
    session: DbSession,
) -> None:
    try:
        await service.change_password(
            principal=principal,
            current_password=body.current_password,
            new_password=body.new_password,
            kc=kc,
            session=session,
        )
    except TooManyAttemptsError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _RATE_LIMITED) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _WRONG_CURRENT) from exc
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
