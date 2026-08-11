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


def test_settings_defaults_match_the_committed_realm() -> None:
    """The alias is a path segment in the broker URL, so a mismatch is a 404 at runtime."""
    s = get_settings().keycloak
    aliases = {i["alias"] for i in _REALM["identityProviders"]}
    assert s.google_idp_alias in aliases
    assert any(c["clientId"] == s.linking_client_id for c in _REALM["clients"])
