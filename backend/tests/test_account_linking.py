"""Cloud-account linking — the Keycloak half of FR-AUT-11 / R-63 (T-214).

What is under test is deliberately narrow: **Corpus asks Keycloak for the provider's
token and never handles a Google credential itself** (R-63(1)). So these tests assert the
broker call's shape and its failure taxonomy, and nothing about Google — there is no Google
client in the application to test.

Keycloak's broker endpoint is stubbed with `respx`, on the same convention as
`test_admin_users.py`. The live half — a real link through a real consent screen — needs
Google OAuth credentials and a running Keycloak, and is recorded as outstanding on T-214.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.auth.keycloak_client import (
    AccountNotLinkedError,
    InvalidCredentialsError,
    KeycloakClient,
    KeycloakRejectedError,
    KeycloakUnavailableError,
)
from app.config import get_settings

_USER_TOKEN = "user-access-token"  # noqa: S105 — a test fixture, not a credential


@pytest.fixture
def kc() -> KeycloakClient:
    return KeycloakClient(get_settings().keycloak)


def _broker_url() -> str:
    s = get_settings().keycloak
    return s.broker_token_endpoint(s.google_idp_alias)


async def test_broker_token_returns_the_providers_token(kc: KeycloakClient, respx_mock) -> None:
    route = respx_mock.get(_broker_url()).respond(
        json={"access_token": "ya29.google-token", "token_type": "Bearer", "expires_in": 3599}
    )

    data = await kc.broker_token(alias="google", user_token=_USER_TOKEN)

    assert data["access_token"] == "ya29.google-token"
    assert route.called


async def test_broker_call_authenticates_as_the_user_not_the_service_account(
    kc: KeycloakClient, respx_mock
) -> None:
    """The token being fetched is the *user's*, and `read-token` is granted to them.

    Sending the service-account token instead would request a token that account does not
    have and earn a 403 that reads as a realm misconfiguration.
    """
    route = respx_mock.get(_broker_url()).respond(json={"access_token": "t"})

    await kc.broker_token(alias="google", user_token=_USER_TOKEN)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {_USER_TOKEN}"


@pytest.mark.parametrize("status", [400, 403])
async def test_unlinked_account_is_not_an_outage(
    kc: KeycloakClient, respx_mock, status: int
) -> None:
    """FR-AUT-11 linking is opt-in, so "not linked" is the ordinary state of most users.

    It must reach the caller as a "link your account" affordance, never as a 5xx.
    """
    respx_mock.get(_broker_url()).respond(status)

    with pytest.raises(AccountNotLinkedError):
        await kc.broker_token(alias="google", user_token=_USER_TOKEN)


async def test_a_200_with_no_token_is_a_configuration_fault(kc: KeycloakClient, respx_mock) -> None:
    """`storeToken` off on the IdP: the link exists but Keycloak kept nothing.

    Not retryable and not the user's problem — it needs a realm change, so it must not be
    reported as unavailability.
    """
    respx_mock.get(_broker_url()).respond(json={"token_type": "Bearer"})

    with pytest.raises(KeycloakRejectedError):
        await kc.broker_token(alias="google", user_token=_USER_TOKEN)


async def test_a_rejected_user_token_is_not_an_unlinked_account(
    kc: KeycloakClient, respx_mock
) -> None:
    """401 is about the caller's own session, not about the provider link."""
    respx_mock.get(_broker_url()).respond(401)

    with pytest.raises(InvalidCredentialsError):
        await kc.broker_token(alias="google", user_token=_USER_TOKEN)


async def test_keycloak_being_down_is_unavailability(kc: KeycloakClient, respx_mock) -> None:
    respx_mock.get(_broker_url()).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(KeycloakUnavailableError):
        await kc.broker_token(alias="google", user_token=_USER_TOKEN)


async def test_a_5xx_from_keycloak_is_unavailability(kc: KeycloakClient, respx_mock) -> None:
    respx_mock.get(_broker_url()).respond(502)

    with pytest.raises(KeycloakUnavailableError):
        await kc.broker_token(alias="google", user_token=_USER_TOKEN)


def test_the_broker_path_is_the_realm_issuer_not_the_admin_api() -> None:
    """`/realms/{realm}/broker/...`, not `/admin/realms/...` — the same 404 trap
    `admin_url` documents, in the other direction."""
    s = get_settings().keycloak
    url = s.broker_token_endpoint("google")
    assert url == f"{s.issuer}/broker/google/token"
    assert "/admin/" not in url


def test_linking_uses_a_different_client_than_ropc_login() -> None:
    """R-63(2): `corpus-backend` must keep `standardFlow` disabled.

    The whole reason a second client exists is that the linking redirect needs the browser
    flow while ROPC login must not — collapsing them would quietly hand the login client a
    redirect flow R-28 deliberately refused.
    """
    s = get_settings().keycloak
    assert s.linking_client_id != s.client_id


# --- The committed realm artifact ------------------------------------------------
# Offline drift guards. `test_auth_live.py` imports this same file into a throwaway realm
# when Keycloak is up; these run always, because the properties below are the ones whose
# silent loss would be worst and hardest to notice.

_REALM = json.loads(
    (Path(__file__).resolve().parents[2] / "deployment/keycloak/corpus-realm.json").read_text(
        encoding="utf-8"
    )
)


def _client(client_id: str) -> dict:
    match = [c for c in _REALM["clients"] if c["clientId"] == client_id]
    assert match, f"{client_id} missing from the committed realm"
    return match[0]


def test_the_realm_enforces_a_password_policy() -> None:
    """NFR-SEC-04's first clause, as a property of the artifact (R-86(1), OI-38).

    T-606 found the realm carrying **no** `passwordPolicy` key at all — not a weak policy but the
    absence of one, so Keycloak enforced no length, complexity, history or reuse rule and any
    password an administrator typed into FR-USR-03's form was accepted. It is filed here rather
    than in the live suite because the live test needs a running Keycloak, and the property whose
    silent loss is worst is the one that must be checked in every run.

    **`forceExpiredPasswordChange` is asserted absent, and that is the load-bearing half.** Under
    backend-mediated ROPC there is no browser (R-28), so any policy that arms a required action
    locks the account out permanently and undiagnosably — §8.9's standing constraint. A rotation
    policy is therefore not a stricter version of this one; it is an outage.
    """
    policy = _REALM.get("passwordPolicy", "")
    assert "length(" in policy, f"no length rule in the password policy: {policy!r}"
    assert "notUsername" in policy, f"the username is still an allowed password: {policy!r}"
    assert "forceExpiredPasswordChange" not in policy, (
        "a rotation policy arms a required action, and under ROPC there is no browser to "
        "complete one — the account is locked out permanently (spec §8.9)"
    )


def test_the_ropc_client_still_refuses_the_browser_flow() -> None:
    """R-28's ROPC-only stance, as a property of the artifact rather than a memory.

    Adding the linking client made it newly plausible for someone to "just enable"
    standardFlow on `corpus-backend` too. That would hand the login client a redirect flow
    R-28 deliberately declined, and nothing else in the tree would notice.
    """
    backend = _client("corpus-backend")
    assert backend["standardFlowEnabled"] is False
    assert backend["directAccessGrantsEnabled"] is True


def test_the_linking_client_carries_the_redirect_and_nothing_else() -> None:
    link = _client("corpus-linking")
    assert link["standardFlowEnabled"] is True
    assert link["directAccessGrantsEnabled"] is False, "linking must never be a login path"
    assert link["fullScopeAllowed"] is False, "linking grants no API access of its own"
    assert link["attributes"]["pkce.code.challenge.method"] == "S256"


def test_google_idp_stores_and_shares_the_token() -> None:
    """Both flags are load-bearing and fail differently.

    Without `storeToken` the broker endpoint answers 200 with no token; without
    `addReadTokenRoleOnCreate` it answers 403, which is indistinguishable from "not linked".
    """
    idp = next(i for i in _REALM["identityProviders"] if i["alias"] == "google")
    assert idp["storeToken"] is True
    assert idp["addReadTokenRoleOnCreate"] is True
    assert idp["enabled"] is True


def test_google_idp_requests_the_drive_scope() -> None:
    idp = next(i for i in _REALM["identityProviders"] if i["alias"] == "google")
    assert "drive.readonly" in idp["config"]["defaultScope"]
    # No refresh token without it, so the brokered token would die in an hour.
    assert idp["config"]["offlineAccess"] == "true"


def test_the_realm_carries_placeholder_google_credentials_only() -> None:
    """A real client secret must never reach the repository (README step 2)."""
    cfg = next(i for i in _REALM["identityProviders"] if i["alias"] == "google")["config"]
    assert cfg["clientId"].startswith("CHANGE_ME_")
    assert cfg["clientSecret"].startswith("CHANGE_ME_")


def test_google_idp_cannot_be_used_to_log_in() -> None:
    """FR-AUT-11 / R-63(3): brokering must never create a Corpus account.

    **This is the test that should have shipped with the provider, and did not.** The realm
    was committed with `linkOnly: false` while three documents — the ruling, FR-AUT-11 and
    `deployment/keycloak/README.md` — all said link-only. Nothing failed, because every
    other realm assertion covered a *different* field.

    `linkOnly` is the enforced half. With it false the provider is offered as a login
    method, and the stock `first broker login` flow runs `idp-create-user-if-unique` as its
    first alternative: a Google identity whose email matches no Corpus user gets one
    **created silently**. That is the self-service signup route FR-USR-02/03 does not have.
    Existing users were unaffected, which is exactly why it looked correct.

    With `linkOnly: true` the provider is hidden from the login page and is reachable only
    from an already-authenticated session, so the identity is linked to *that* user and
    there is no unique-user branch to take.
    """
    idp = next(i for i in _REALM["identityProviders"] if i["alias"] == "google")
    assert idp["linkOnly"] is True, (
        "the Google provider must not be a login method — see FR-AUT-11 and R-63(3)"
    )


def test_the_first_broker_login_flow_exists() -> None:
    """A flow alias naming nothing is an import-time failure, not a runtime one.

    The realm currently names Keycloak's built-in flow. Should it ever name a custom
    link-only flow (the defence-in-depth hardening T-214 still owes), that flow has to be
    defined in this same artifact or the import breaks for everyone.
    """
    idp = next(i for i in _REALM["identityProviders"] if i["alias"] == "google")
    alias = idp["firstBrokerLoginFlowAlias"]
    builtin = {"first broker login"}
    defined = {f["alias"] for f in _REALM.get("authenticationFlows", [])}
    assert alias in builtin | defined, f"{alias!r} is neither built-in nor defined in the realm"


def test_the_seeded_admin_holds_the_broker_read_token_role() -> None:
    """`addReadTokenRoleOnCreate` is INERT under `linkOnly: true` — proved live 2026-08-11.

    R-63, FR-AUT-11 and the Keycloak README all said the `broker` `read-token` role is
    "granted at link time by `addReadTokenRoleOnCreate`". **It is not.** That setting fires
    only when brokering *creates* an account, and `linkOnly: true` exists precisely to
    forbid that — so the two are mutually exclusive in effect and the role is never granted.

    The consequence was total and silent: after a completely successful link (federated
    identity present, consent granted, Google token stored), `broker_token()` raised
    `AccountNotLinkedError` — **misreporting the cause**, because Keycloak answers 403 both
    for "not linked" and for "missing read-token", which `AccountNotLinkedError`'s own
    docstring notes are indistinguishable. Every user would have hit this forever.

    No mocked test could have found it: the double supplies the very behaviour under test.
    Only a real consent round-trip does.

    `read-token` is safe to hold unconditionally — it permits reading *your own* brokered
    token and yields nothing at all without a federated link.

    **Do not try to grant this through the realm's default roles — it was tried and it does
    not work.** Making our `user` role a composite carrying `client: {broker: [read-token]}`
    fails the realm import outright with a bare `500`, because realm roles are created
    *before* Keycloak's built-in clients (`broker`, `account`) exist, so the composite
    reference cannot resolve. Per-user `clientRoles` works only because users are imported
    *after* clients. That is why this grant is per-user here, and why granting it to **every**
    user is T-214's job in the linking endpoint (after a successful link, via the service
    account) rather than a line of realm configuration.
    """
    admin = next(u for u in _REALM["users"] if u["username"] == "admin@corpus.local")
    assert admin.get("clientRoles", {}).get("broker") == ["read-token"], (
        "the seeded admin must hold `broker` read-token — addReadTokenRoleOnCreate cannot "
        "grant it while the provider is linkOnly"
    )


def test_the_seeded_admin_can_use_the_account_console() -> None:
    """The seeded admin must not be silently unlike every other user.

    A user's `realmRoles` in a realm import **replaces** the defaults rather than adding to
    them, so declaring `['admin', 'user']` cost the seeded admin `default-roles-<realm>` —
    and with it `view-profile` and `manage-account`. Users created through Corpus's own
    `POST /api/v1/users` get that composite from Keycloak automatically, so the *seeded*
    admin was the only user in the realm without it. It surfaced as the Keycloak account
    console failing with a bare "Something went wrong", which names nothing.

    **Granted through the `account` client rather than the composite, deliberately.**
    `default-roles-<realm>` embeds the realm's name, and `test_realm_artifact_imports_and_works`
    imports this artifact into a *renamed* throwaway realm — so a hardcoded composite name
    resolves to nothing there, trading a live defect for a CI one. The `account` client
    exists in every realm under a fixed id, so this form is portable; verified by importing
    into a renamed realm and reading the effective roles back.

    `manage-account` carries `manage-account-links` as a composite, which is what actually
    permits identity-provider linking.

    Not restored: `offline_access` and `uma_authorization`. Both are unused by Corpus —
    offline tokens are never requested and authorization services are not enabled — so they
    are deliberately out of scope rather than overlooked.
    """
    admin = next(u for u in _REALM["users"] if u["username"] == "admin@corpus.local")
    account_roles = set(admin.get("clientRoles", {}).get("account", []))
    assert {"view-profile", "manage-account"} <= account_roles, (
        "the seeded admin cannot use the account console without view-profile/manage-account"
    )


def test_the_service_account_can_resolve_the_broker_client() -> None:
    """`view-clients`, without which the linking flow cannot grant `read-token` (T-214).

    The grant needs the `broker` client's internal uuid, and resolving it is
    `GET /clients?clientId=broker`. **The near-miss is the reason this is pinned:** the
    lighter `query-clients` role answers that call with **`200 []`** rather than `403`
    (measured against the running realm), so a realm short of `view-clients` would report the
    `broker` client as *not existing* — and the linking flow would fail with a message about a
    missing client rather than a missing permission.

    `KeycloakClient.admin_get_client_uuid` therefore raises on the empty list instead of
    treating it as absence, and this test keeps the role that makes the list non-empty.
    """
    account = next(u for u in _REALM["users"] if u["username"] == "service-account-corpus-backend")
    assert "view-clients" in account["clientRoles"]["realm-management"], (
        "the backend's service account cannot resolve the `broker` client, so it cannot grant "
        "`read-token` and every link would be inert — see T-214"
    )


def test_settings_defaults_match_the_committed_realm() -> None:
    """The alias is a path segment in the broker URL, so a mismatch is a 404 at runtime."""
    s = get_settings().keycloak
    aliases = {i["alias"] for i in _REALM["identityProviders"]}
    assert s.google_idp_alias in aliases
    assert any(c["clientId"] == s.linking_client_id for c in _REALM["clients"])
