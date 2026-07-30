"""Thin httpx wrapper over Keycloak's token + admin endpoints (R-28, T-103).

Backend-mediated ROPC: the frontend never talks to Keycloak. This isolates every
Keycloak HTTP call behind one class so the later Admin-API task (T-104,
``python-keycloak``) is a drop-in swap. No ``python-keycloak`` dependency here —
change-password (self-service) needs only the token endpoint plus one admin
``reset-password`` call, both plain httpx.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.config import KeycloakSettings

log = structlog.get_logger(__name__)

_TIMEOUT = 15.0
_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


class KeycloakError(Exception):
    """Base for Keycloak call failures."""


class InvalidCredentialsError(KeycloakError):
    """Wrong username/password or invalid/expired refresh token → 401."""


class TooManyAttemptsError(KeycloakError):
    """Account temporarily disabled by brute-force protection → 429."""


class KeycloakUnavailableError(KeycloakError):
    """Keycloak was unreachable, timed out, or failed server-side (5xx) → 503.

    **Retryable, and that is the whole meaning of the class** (T-110). `503` promises the
    caller and every proxy between "transient, try later", so only conditions that can
    actually clear on their own belong here. Everything that needs a human to change
    configuration is :class:`KeycloakForbiddenError` or :class:`KeycloakRejectedError`.
    """


class KeycloakForbiddenError(KeycloakError):
    """The Admin API refused **our own** service-account credentials (401/403) → 500.

    Not the caller's problem: these routes are already admin-gated, so the request reaching
    Keycloak was authorized by us — what failed is the backend's own credential. Two causes,
    one remedy shape: the service account lacks a `realm-management` role (403), or its token
    was rejected because `KEYCLOAK_CLIENT_SECRET` is wrong or the account is disabled (401).

    **This class exists because T-110 lost time to its absence.** A 403 here used to become
    `KeycloakUnavailableError` → `503 "Authentication service unavailable."`, so an
    under-provisioned service account was reported as an outage: `POST /users` worked while
    `GET /users` "failed" — a combination no real outage can produce, and the only clue that
    the message was wrong. Passing the 403 through to the caller would be the *other* lie
    (they are authorized; we are not), hence 500.
    """


class KeycloakRejectedError(KeycloakError):
    """Keycloak rejected the request *we* built (400/422) → 500.

    A bug on our side — a malformed representation, an unsupported field — and therefore
    never something the caller can act on and never retryable. Kept apart from
    :class:`KeycloakForbiddenError` because the operator's next step differs completely
    (read the payload we sent vs grant a role), even though both render the same 500.
    """


class UserNotFoundError(KeycloakError):
    """Admin API targeted a user id that does not exist → 404."""


class UserConflictError(KeycloakError):
    """Admin create hit a duplicate username/email → 409."""


class KeycloakClient:
    def __init__(self, settings: KeycloakSettings) -> None:
        self._kc = settings
        self._base_form = {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
        }

    async def password_grant(self, username: str, password: str) -> dict[str, Any]:
        """ROPC login. Raises on bad credentials / locked account."""
        return await self._token(
            {
                **self._base_form,
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": "openid",
            }
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._token(
            {**self._base_form, "grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def logout(self, refresh_token: str) -> None:
        """Revoke the refresh token / end the Keycloak session (idempotent)."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    self._kc.logout_endpoint,
                    data={**self._base_form, "refresh_token": refresh_token},
                    headers=_FORM_HEADERS,
                )
        except httpx.HTTPError as exc:
            raise KeycloakUnavailableError(str(exc)) from exc
        # 204 = ended; 400 = already invalid — both are "logged out" for us.
        if resp.status_code not in (200, 204, 400):
            raise KeycloakUnavailableError(f"logout returned {resp.status_code}")

    async def service_account_token(self) -> str:
        """client_credentials grant → access token for admin calls."""
        data = await self._token({**self._base_form, "grant_type": "client_credentials"})
        token = data.get("access_token")
        if not token:
            raise KeycloakUnavailableError("client_credentials returned no access_token")
        return token

    async def admin_reset_password(self, *, sub: str, new_password: str, admin_token: str) -> None:
        """Set a user's password via the Admin API (FR-USR-05, FR-USR-09). The
        service account must hold realm-management ``manage-users``."""
        await self._admin(
            "PUT",
            f"/users/{sub}/reset-password",
            admin_token=admin_token,
            json={"type": "password", "value": new_password, "temporary": False},
            expected=(204,),
        )

    async def admin_create_user(
        self, *, email: str, display_name: str | None, password: str, admin_token: str
    ) -> str:
        """Create a user and return the new Keycloak ``sub`` (FR-USR-03).

        The realm's ``defaultRoles`` grant ``user``; promote to ``admin`` via a
        separate role-mapping call. Duplicate email/username → ``UserConflictError``.
        """
        payload: dict[str, Any] = {
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
            **_name_fields(display_name),
        }
        resp = await self._admin(
            "POST", "/users", admin_token=admin_token, json=payload, expected=(201,)
        )
        sub = resp.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
        if not sub:
            raise KeycloakUnavailableError("create user returned no Location header")
        return sub

    async def admin_list_users(
        self, *, admin_token: str, first: int = 0, limit: int = 100, search: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"first": first, "max": limit}
        if search:
            params["search"] = search
        resp = await self._admin(
            "GET", "/users", admin_token=admin_token, params=params, expected=(200,)
        )
        return resp.json()

    async def admin_get_user(self, *, sub: str, admin_token: str) -> dict[str, Any]:
        """Fetch a single user representation. 404 → UserNotFoundError."""
        resp = await self._admin("GET", f"/users/{sub}", admin_token=admin_token, expected=(200,))
        return resp.json()

    async def admin_get_role_users(
        self, *, role_name: str, admin_token: str
    ) -> list[dict[str, Any]]:
        """Users holding a realm role — one call resolves admin membership for a list."""
        resp = await self._admin(
            "GET", f"/roles/{role_name}/users", admin_token=admin_token, expected=(200,)
        )
        return resp.json()

    async def admin_get_user_realm_roles(
        self, *, sub: str, admin_token: str
    ) -> list[dict[str, Any]]:
        """A user's assigned realm roles. 404 → UserNotFoundError."""
        resp = await self._admin(
            "GET", f"/users/{sub}/role-mappings/realm", admin_token=admin_token, expected=(200,)
        )
        return resp.json()

    async def admin_update_user(self, *, sub: str, admin_token: str, **fields: Any) -> None:
        """Partial user update (email/firstName/lastName/enabled). 404 → UserNotFoundError."""
        await self._admin(
            "PUT", f"/users/{sub}", admin_token=admin_token, json=fields, expected=(204,)
        )

    async def admin_delete_user(self, *, sub: str, admin_token: str) -> None:
        """Hard-delete a user (FR-USR-03). 404 → UserNotFoundError."""
        await self._admin("DELETE", f"/users/{sub}", admin_token=admin_token, expected=(204,))

    async def admin_get_realm_role(self, *, name: str, admin_token: str) -> dict[str, Any]:
        """Fetch a realm role representation (id + name) for role-mapping calls."""
        resp = await self._admin("GET", f"/roles/{name}", admin_token=admin_token, expected=(200,))
        return resp.json()

    async def admin_add_realm_roles(
        self, *, sub: str, roles: list[dict[str, Any]], admin_token: str
    ) -> None:
        await self._admin(
            "POST",
            f"/users/{sub}/role-mappings/realm",
            admin_token=admin_token,
            json=roles,
            expected=(204,),
        )

    async def admin_remove_realm_roles(
        self, *, sub: str, roles: list[dict[str, Any]], admin_token: str
    ) -> None:
        await self._admin(
            "DELETE",
            f"/users/{sub}/role-mappings/realm",
            admin_token=admin_token,
            json=roles,
            expected=(204,),
        )

    async def _admin(
        self,
        method: str,
        path: str,
        *,
        admin_token: str,
        expected: tuple[int, ...],
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform an Admin REST call under ``admin_url`` with common error mapping."""
        url = f"{self._kc.admin_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
        except httpx.HTTPError as exc:
            raise KeycloakUnavailableError(str(exc)) from exc
        if resp.status_code == 404:
            raise UserNotFoundError(path)
        if resp.status_code == 409:
            raise UserConflictError(_conflict_detail(resp))
        if resp.status_code not in expected:
            # T-110: discriminate before falling through. The catch-all used to file every
            # unexpected status as `Unavailable` → 503, which told operators the auth service
            # was down whenever our own service account was short a role. The log line is part
            # of the fix, not decoration: it is the only place the offending method, path and
            # status appear, and its absence is what made the original diagnosis three probes
            # long instead of one.
            log.error(
                "keycloak.admin_call_failed",
                method=method,
                path=path,
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
            if resp.status_code in (401, 403):
                raise KeycloakForbiddenError(
                    f"{method} {path} returned {resp.status_code}: the backend's Keycloak "
                    "service account is missing a realm-management role, or its credentials "
                    "were rejected (see deployment/keycloak/README.md)"
                )
            if resp.status_code in (400, 422):
                raise KeycloakRejectedError(f"{method} {path} returned {resp.status_code}")
            # Anything left is either an upstream 5xx or a status we did not enumerate in
            # `expected` — both genuinely "unexpected", so both keep the retryable class.
            raise KeycloakUnavailableError(f"{method} {path} returned {resp.status_code}")
        return resp

    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(self._kc.token_endpoint, data=data, headers=_FORM_HEADERS)
        except httpx.HTTPError as exc:
            raise KeycloakUnavailableError(str(exc)) from exc

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (400, 401):
            self._raise_token_error(resp)
        raise KeycloakUnavailableError(f"token endpoint returned {resp.status_code}")

    @staticmethod
    def _raise_token_error(resp: httpx.Response) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        description = str(body.get("error_description", "")).lower()
        # Keycloak masks brute-force lockout as invalid_grant (anti-enumeration);
        # the description is the only tell. This is a best-effort 429 *backstop* —
        # the authoritative app-level throttle is now slowapi (T-105,
        # app.security.rate_limit), which returns the same FR-AUT-04 copy. # TBD(§8.4)
        if "disabled" in description or "too many" in description:
            raise TooManyAttemptsError(description)
        raise InvalidCredentialsError(body.get("error", "invalid_grant"))


def _name_fields(display_name: str | None) -> dict[str, str]:
    """Split a display name into Keycloak ``firstName``/``lastName`` (first space)."""
    if not display_name:
        return {}
    first, _, last = display_name.partition(" ")
    return {"firstName": first, "lastName": last}


def _conflict_detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("errorMessage", "conflict"))
    except ValueError:
        return "conflict"
