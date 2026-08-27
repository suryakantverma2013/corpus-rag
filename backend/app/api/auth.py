"""``/auth`` routes (R-25 surface, R-28 Keycloak-backed) — T-103.

Thin handlers: orchestration lives in ``app.auth.service`` / ``KeycloakClient``.
This layer only maps Keycloak error subclasses to HTTP status codes and the
spec's copy strings (FR-AUT-04/06/08/09).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.errors import MISCONFIGURED, RATE_LIMITED, UPSTREAM_DOWN, error_responses
from app.auth import service
from app.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession, Keycloak
from app.auth.keycloak_client import (
    InvalidCredentialsError,
    KeycloakForbiddenError,
    KeycloakRejectedError,
    KeycloakUnavailableError,
    PasswordPolicyError,
    TooManyAttemptsError,
)
from app.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    TokenResponse,
)
from app.config import get_settings
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


# FR-AUT-06's copy, reached when the refresh cookie is missing, stale or rejected. Identical
# for all three, deliberately: the client's response is the same in every case (return to the
# login screen), and distinguishing them would tell an unauthenticated caller whether a given
# cookie value was ever a real session (NFR-SEC-02).
_SESSION_EXPIRED = "Invalid or expired session."


def _token_response(response: Response, tokens: dict[str, Any]) -> TokenResponse:
    """Split Keycloak's token payload: access token to the body, refresh token to the cookie.

    R-72(1)/FR-AUT-07. This is the only place a refresh token crosses into a response, which
    is what makes "the browser cannot read it" checkable rather than a convention.
    """
    _set_refresh_cookie(response, tokens["refresh_token"], tokens.get("refresh_expires_in"))
    return TokenResponse(
        access_token=tokens["access_token"],
        token_type="bearer",
        expires_in=int(tokens.get("expires_in", 0)),
    )


def _set_refresh_cookie(response: Response, refresh_token: str, expires_in: Any) -> None:
    """Write the refresh cookie, expiring it exactly when the credential it carries expires.

    ``max_age`` is Keycloak's own ``refresh_expires_in`` rather than a setting of ours. On the
    shipped realm that is ``ssoSessionIdleTimeout`` (1800s), which is the *inactivity timeout*
    OI-09 left open — so the browser drops the cookie at the moment the session dies, and an
    operator who retunes the realm needs no corresponding change here (R-72(2)).

    A non-positive or absent value means Keycloak declined to bound it (offline tokens do
    this), and the cookie falls back to a **session cookie** rather than to a number we
    invented — the browser then discards it when it closes, which is the conservative end of
    the two and is consistent with R-27's "no remember me".
    """
    settings = get_settings().session
    try:
        max_age: int | None = int(expires_in)
    except TypeError, ValueError:
        max_age = None
    if max_age is not None and max_age <= 0:
        max_age = None
    response.set_cookie(
        settings.refresh_cookie_name,
        refresh_token,
        max_age=max_age,
        path=settings.refresh_cookie_path,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Remove the cookie. The path MUST match the one it was set with, or the browser keeps
    the original and the user stays signed in with a revoked token in the jar."""
    settings = get_settings().session
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
    )


def _clearing_headers() -> dict[str, str]:
    """The same ``Set-Cookie``, as a header dict for an :class:`HTTPException`.

    **The injected ``Response`` is discarded when a handler raises** — FastAPI builds the
    error response from the exception, so anything written onto ``response`` before the
    ``raise`` never reaches the browser. Every failure path here is one where the cookie must
    go (a rejected refresh, an unreachable revoke), so those paths carry the header on the
    exception itself. Asserted by ``test_auth.py``, not assumed.
    """
    probe = Response()
    _clear_refresh_cookie(probe)
    return {"set-cookie": probe.headers["set-cookie"]}


def _refresh_cookie(request: Request) -> str:
    settings = get_settings().session
    token = request.cookies.get(settings.refresh_cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _SESSION_EXPIRED)
    return token


def _expired() -> HTTPException:
    """A 401 that also takes the spent cookie away."""
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, _SESSION_EXPIRED, headers=_clearing_headers()
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        **error_responses((401, "Invalid email or password (FR-AUT-04).")),
        **RATE_LIMITED,
        **UPSTREAM_DOWN,
    },
    summary="Exchange credentials for a token pair",
)
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
    return _token_response(response, tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={
        **error_responses((401, "The refresh cookie is absent, invalid or expired.")),
        **RATE_LIMITED,
        **UPSTREAM_DOWN,
    },
    summary="Exchange the refresh cookie for a new access token",
)
@limiter.limit(refresh_limit, key_func=client_ip_key)
async def refresh(
    request: Request, response: Response, kc: Keycloak, session: DbSession
) -> TokenResponse:
    """No request body: the refresh token comes from the httpOnly cookie and nowhere else
    (R-72(1)). The rotated token replaces the cookie, so an idle client's cookie lifetime
    tracks the realm's ``ssoSessionIdleTimeout`` rather than the login time."""
    cookie = _refresh_cookie(request)
    try:
        tokens = await service.refresh(refresh_token=cookie, kc=kc, session=session)
    except InvalidCredentialsError as exc:
        # The cookie is spent — take it away, or every subsequent request re-sends a token
        # that can only ever 401 again, and the browser's jar disagrees with the server about
        # whether a session exists.
        raise _expired() from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
    return _token_response(response, tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=UPSTREAM_DOWN,
    summary="Revoke the session and clear the refresh cookie",
)
async def logout(request: Request, response: Response, kc: Keycloak, session: DbSession) -> None:
    """Idempotent, and the cookie is cleared on **every** path including the ones that fail.

    FR-AUT-08's sign out must leave the browser signed out even when the upstream revoke
    cannot be delivered; the alternative is a user who clicked "Sign out", saw an error, and
    is still holding a live session cookie.
    """
    settings = get_settings().session
    refresh_token = request.cookies.get(settings.refresh_cookie_name)
    _clear_refresh_cookie(response)
    if not refresh_token:
        return
    try:
        await service.logout(refresh_token=refresh_token, kc=kc, session=session)
    except InvalidCredentialsError:
        # Already revoked or expired upstream: the caller's intent is satisfied and the
        # cookie is gone. Reporting an error here would be reporting success as a failure.
        return
    except KeycloakUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM, headers=_clearing_headers()
        ) from exc


@router.get("/me", response_model=MeResponse, summary="The caller's own profile")
async def me(principal: CurrentPrincipal, user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=sorted(role.value for role in principal.roles),
        is_active=user.is_active,
    )


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **error_responses((401, "The current password is incorrect.")),
        **RATE_LIMITED,
        **MISCONFIGURED,
        **UPSTREAM_DOWN,
    },
    summary="Change the caller's own password",
)
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
    except PasswordPolicyError as exc:
        # 400 with Keycloak's own wording (B-004). Someone changing their own password needs to
        # know WHICH rule the new one broke; the 500 this used to raise said only that the
        # server was misconfigured, which is both wrong and unactionable.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
