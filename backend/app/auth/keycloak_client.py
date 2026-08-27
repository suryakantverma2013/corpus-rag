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


class BrokerGrantExpiredError(KeycloakError):
    """The link exists, but the provider's grant can no longer be refreshed (B-008).

    Kept apart from :class:`AccountNotLinkedError` because the two are different facts and
    R-63(6) gave them different codes on purpose: *no link was ever made* versus *the link is
    there and the provider refused*. The status route answers `200 linked` in this case, so
    reporting "not linked" would have the product contradict itself in two calls.
    """


class PasswordPolicyError(KeycloakError):
    """The password the *caller* supplied violates the realm's policy (400) → 400.

    Split out of :class:`KeycloakRejectedError` by B-004. Both are Keycloak 400s, but they are
    opposite kinds of fault: `Rejected` means *we* built a bad request and the operator must
    read our payload, while this means the **caller typed a password the policy refuses** and
    the fix is to type a different one. Reporting the second as the first produced a `500`
    telling an administrator to check the server logs for a short password.

    Carries Keycloak's own `error_description`, which names the failing clause.
    """


class AccountNotLinkedError(KeycloakError):
    """The user has not linked this identity provider, so no brokered token exists.

    Distinct from every other error here because it is **the caller's to fix and the fix is
    a UI affordance**, not an outage and not a bug: FR-AUT-11 linking is opt-in, so "not
    linked" is the ordinary state of every user who has never asked for Drive. It maps to a
    "link your account" response, never to a 5xx.

    Keycloak answers 400 or 403 for this, and 403 is also what it returns when the user
    lacks the `broker` `read-token` role — a realm-configuration fault. The two are not
    distinguishable from the status alone.

    **That ambiguity bit for real on 2026-08-11, and the mitigation this docstring used to
    claim does not work.** It said the IdP's `addReadTokenRoleOnCreate` grants the role at
    link time, so a 403 must mean unlinked. It does not: that setting fires only when
    brokering *creates* an account, and the provider is `linkOnly: true` precisely to forbid
    that — **the two settings are mutually exclusive in effect**. After a fully successful
    link (federated identity present, Google token stored), this error was raised anyway and
    named the wrong cause. The realm now grants `read-token` explicitly; if this is ever seen
    against a user you know is linked, check the role before believing the message.
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

    async def broker_token(self, *, alias: str, user_token: str) -> dict[str, Any]:
        """The **provider's** current access token for this user (FR-AUT-11, R-63(1)).

        This is the whole reason cloud import costs so little: Keycloak performed the OAuth
        exchange, stores the provider's tokens (`storeToken`) and refreshes them, so Corpus
        holds no third-party credential, needs no token table, and inherits Keycloak's
        revocation. We ask; we do not manage.

        Authenticated with the **end user's** access token, not the service account — the
        token being fetched is that user's, and the `broker` `read-token` role is held by
        them. Passing the service-account token here would ask for a token the service
        account does not have and get a 403 that looks like a misconfiguration.

        That role is granted by `app.services.cloud_links.grant_read_token` when the link
        completes, **not** by the IdP's `addReadTokenRoleOnCreate` — see
        :class:`AccountNotLinkedError` for why the difference cost a live debugging session.

        The response shape is the provider's own token response, so callers must read
        ``access_token`` and must not assume a refresh token is present.
        """
        url = self._kc.broker_token_endpoint(alias)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {user_token}"})
        except httpx.HTTPError as exc:
            raise KeycloakUnavailableError(str(exc)) from exc

        if resp.status_code == 200:
            data = resp.json()
            if not data.get("access_token"):
                # A 200 with no token means the link exists but Keycloak stored nothing —
                # `storeToken` off on the IdP. A configuration fault, not an outage.
                raise KeycloakRejectedError(f"broker/{alias} returned no access_token")
            return data
        if resp.status_code in (400, 403):
            raise AccountNotLinkedError(alias)
        if resp.status_code == 401:
            # Our caller's token was rejected — the user's session, not the link.
            raise InvalidCredentialsError("user token rejected by the broker endpoint")
        # A link whose provider grant can no longer be refreshed (B-008). Keycloak signals this
        # with **502 `{"errorMessage":"Unable to refresh token"}`** — measured, not guessed —
        # which fell through to `Unavailable` and told the user *"Authentication service
        # unavailable."* while Keycloak was demonstrably up and `GET /cloud/links/google`
        # answered `200 linked`. It is R-63(6)'s `CLOUD_ACCESS_REVOKED` case exactly: the link
        # exists and the *provider* refused, so the user must re-link.
        #
        # Narrowed to that message rather than to the status, deliberately: a bare 502 from a
        # proxy in front of Keycloak is a real outage, and mapping every 5xx to "re-link" would
        # send users to redo their consent during an incident. If the message ever changes, the
        # symptom returns as a 503 — the status quo, not a new failure.
        if resp.status_code == 502 and "unable to refresh token" in resp.text.lower():
            raise BrokerGrantExpiredError(alias)
        raise KeycloakUnavailableError(f"broker/{alias} returned {resp.status_code}")

    async def exchange_linking_code(
        self, *, code: str, redirect_uri: str, verifier: str
    ) -> dict[str, Any]:
        """Redeem leg 1's authorization code on the **linking** client (T-214, FR-AUT-11).

        `corpus-linking`, never `client_id`/`client_secret`: that client is public, carries
        `standardFlow` and nothing else, and grants no API access of its own
        (`fullScopeAllowed: false`). Sending the ROPC client's credentials here would both
        fail — it has no browser flow — and blur the separation R-63(2) exists to keep.

        The response is wanted for two things and only two: the ``id_token``, whose ``sub``
        proves *who* authenticated in that browser, and ``session_state``, which leg 2's
        link hash is computed over. No token from here is ever stored or returned to a
        client.
        """
        return await self._token(
            {
                "client_id": self._kc.linking_client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def admin_list_federated_identities(
        self, *, sub: str, admin_token: str
    ) -> list[dict[str, Any]]:
        """The user's linked identity providers (FR-AUT-11's "report linked state").

        The authoritative answer, and deliberately not "does `broker_token()` succeed?" —
        that call conflates an absent link with a missing `read-token` role, which is the
        exact ambiguity :class:`AccountNotLinkedError` documents and T-214 was bitten by.
        """
        resp = await self._admin(
            "GET", f"/users/{sub}/federated-identity", admin_token=admin_token, expected=(200,)
        )
        return resp.json()

    async def admin_remove_federated_identity(
        self, *, sub: str, alias: str, admin_token: str
    ) -> None:
        """Unlink a provider (FR-AUT-11). Idempotent: an absent link is not an error.

        Keycloak answers 404 for "there was no such link", which `_admin` raises as
        `UserNotFoundError` — swallowed here, because the caller asked for the link to be
        gone and it is. Documents already imported are untouched by design: FR-KBM-10 makes
        them copies, so unlinking revokes *future* import and nothing else.
        """
        try:
            await self._admin(
                "DELETE",
                f"/users/{sub}/federated-identity/{alias}",
                admin_token=admin_token,
                expected=(204,),
            )
        except UserNotFoundError:
            log.info("keycloak.unlink_noop", alias=alias)

    async def admin_get_client_uuid(self, *, client_id: str, admin_token: str) -> str:
        """Resolve a client's *internal* uuid from its `clientId` (T-214).

        Needed because role-mapping paths are keyed on the uuid, not the name.

        **This call needs `view-clients`, and the near-miss is worth naming**: with
        `query-clients` instead, Keycloak answers **`200 []`** rather than `403` — a
        permission gap that reads as "the `broker` client does not exist" (measured, T-214).
        Hence the empty list is raised as a misconfiguration rather than returned as absence.
        """
        resp = await self._admin(
            "GET",
            "/clients",
            admin_token=admin_token,
            params={"clientId": client_id},
            expected=(200,),
        )
        clients = resp.json()
        if not clients:
            raise KeycloakForbiddenError(
                f"no client named {client_id!r} was visible to the backend's service account "
                "— grant it realm-management `view-clients` (`query-clients` answers 200 with "
                "an empty list, which is indistinguishable from the client not existing); "
                "see deployment/keycloak/README.md"
            )
        return str(clients[0]["id"])

    async def admin_get_client_role(
        self, *, client_uuid: str, role_name: str, admin_token: str
    ) -> dict[str, Any]:
        resp = await self._admin(
            "GET",
            f"/clients/{client_uuid}/roles/{role_name}",
            admin_token=admin_token,
            expected=(200,),
        )
        return resp.json()

    async def admin_add_client_roles(
        self, *, sub: str, client_uuid: str, roles: list[dict[str, Any]], admin_token: str
    ) -> None:
        """Assign client roles to a user. Idempotent — re-adding an existing mapping is a 204."""
        await self._admin(
            "POST",
            f"/users/{sub}/role-mappings/clients/{client_uuid}",
            admin_token=admin_token,
            json=roles,
            expected=(204,),
        )

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
        self, *, role_name: str, admin_token: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """**Every** user holding a realm role, following Keycloak's pagination.

        `list_users` uses this to resolve admin membership for a whole page in one pass, and
        it is the *complete* set that makes that safe. The call used to send no `first`/`max`
        and take whatever Keycloak's default page was — while `GET /api/v1/users` accepts a
        `limit` of up to **500**. On a realm with more administrators than that default, every
        admin past it was rendered **as a non-administrator**: a security-relevant answer, and
        a silent one, since a short list is indistinguishable from a complete one.

        Paginating explicitly is correct regardless of what the server's default happens to be
        — which is the point, because that default is not ours to pin.

        Same family as `admin_get_client_uuid`'s empty-list guard: **a collection response is
        not evidence of the collection's size.** There the risk is "no permission" reading as
        "does not exist"; here it is "page one" reading as "all of them".
        """
        users: list[dict[str, Any]] = []
        first = 0
        # A bound, not a limit: at `page_size` 100 this admits 100k role holders, far past any
        # realistic realm, and stops a server that always returns a full page from spinning
        # this loop forever.
        for _ in range(1000):
            resp = await self._admin(
                "GET",
                f"/roles/{role_name}/users",
                admin_token=admin_token,
                params={"first": first, "max": page_size},
                expected=(200,),
            )
            page = resp.json()
            users.extend(page)
            if len(page) < page_size:
                return users
            first += page_size
        log.error("keycloak.role_users_page_limit", role=role_name, collected=len(users))
        return users

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
                # A password-policy rejection is a 400 too, and it is NOT our malformed request
                # — it is the caller's input (B-004). Before this split, an administrator who
                # typed a short password was told "User administration is not configured
                # correctly on the server. Check the server logs", which sends them to debug the
                # deployment for a typo. It was unreachable until R-86(1) put a `passwordPolicy`
                # in the realm artifact, so it ships live in every deployment created since.
                #
                # Detected from Keycloak's own body rather than from the status, measured:
                # `{"error":"invalidPasswordMinLengthMessage",
                #   "error_description":"Invalid password: minimum length 12."}`
                # Every policy failure uses the `invalidPassword…` prefix, which is why the
                # prefix is the test and not a list of message keys that would need extending
                # each time a policy clause is added.
                policy = _password_policy_detail(resp)
                if policy is not None:
                    raise PasswordPolicyError(policy)
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


def _password_policy_detail(resp: httpx.Response) -> str | None:
    """Keycloak's own words for a password-policy rejection, or `None` if it is not one.

    Returning Keycloak's `error_description` verbatim is deliberate: it names the clause that
    failed ("Invalid password: minimum length 12."), which is exactly what the administrator
    needs and what a generic string would withhold. It is safe to echo here because the policy
    is the operator's own configuration, not a secret, and every route reaching this is
    admin-gated.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if not str(body.get("error", "")).startswith("invalidPassword"):
        return None
    return str(body.get("error_description") or "The password does not meet the realm policy.")
