"""The public/internal Keycloak URL split (T-610, R-82).

`KeycloakSettings` derives eight URLs from what used to be a single `server_url`, and they
have two different readers:

  * a **browser** follows `authorization_endpoint` and `account_link_endpoint`, and the
    `iss` claim it carries back is compared against `issuer`;
  * the **backend** fetches `jwks_uri`, `token_endpoint`, `logout_endpoint`, `admin_url`
    and `broker_token_endpoint`.

In a container deployment those two readers resolve different names for the same Keycloak,
so one setting cannot serve both: address it internally and the FR-AUT-11 linking redirect
sends the browser to a name it cannot resolve; address it publicly and every server-to-server
call hairpins out through the edge and back.

The tests below pin **which side each URL is on**, because that assignment is the whole
feature and every one of them is a plausible-looking one-word edit.
"""

from __future__ import annotations

import pytest

from app.config import KeycloakSettings

PUBLIC = "https://corpus.example.com/auth"
INTERNAL = "http://keycloak:8080/auth"


def _split() -> KeycloakSettings:
    return KeycloakSettings(server_url=PUBLIC, internal_url=INTERNAL, realm="corpus")


def _single() -> KeycloakSettings:
    return KeycloakSettings(server_url=PUBLIC, realm="corpus")


# --- the default is unchanged -------------------------------------------------------


def test_an_unset_internal_url_leaves_every_endpoint_on_the_public_host() -> None:
    """The single-host deployment must be bit-for-bit what it was before R-82.

    `internal_url` is additive: a deployment that never sets it can see no difference,
    which is what makes the change safe to land under an existing realm.
    """
    kc = _single()

    assert kc.internal_issuer == kc.issuer
    for url in (
        kc.jwks_uri,
        kc.token_endpoint,
        kc.logout_endpoint,
        kc.admin_url,
        kc.broker_token_endpoint("google"),
        kc.authorization_endpoint(),
        kc.account_link_endpoint("google"),
    ):
        assert url.startswith(PUBLIC), url


def test_an_empty_internal_url_is_the_same_as_an_unset_one() -> None:
    """`KEYCLOAK_INTERNAL_URL=` in an env file must mean "unset", not "empty host".

    Asserted against the expected literal rather than against `_single()`. Comparing the
    two configs is what this test did first, and it was **vacuous**: the default for
    `internal_url` is `""`, so both sides were the same input and a mutation that broke
    the empty case broke both halves identically and kept them equal. Caught by mutation
    testing, and the reason this reads the way it does.
    """
    kc = KeycloakSettings(server_url=PUBLIC, internal_url="", realm="corpus")

    assert kc.internal_issuer == f"{PUBLIC}/realms/corpus"
    assert kc.token_endpoint == f"{PUBLIC}/realms/corpus/protocol/openid-connect/token"
    assert kc.admin_url == f"{PUBLIC}/admin/realms/corpus"


# --- the split, by reader -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["jwks_uri", "token_endpoint", "logout_endpoint", "admin_url"],
)
def test_server_to_server_urls_follow_the_internal_host(name: str) -> None:
    """These are fetched by the API process, which resolves the service name."""
    assert getattr(_split(), name).startswith(INTERNAL)


def test_the_broker_token_endpoint_is_server_to_server() -> None:
    """FR-AUT-11 reads the *provider's* access token from Keycloak in-process."""
    assert _split().broker_token_endpoint("google").startswith(INTERNAL)


def test_browser_redirects_stay_on_the_public_host() -> None:
    """A browser cannot resolve a container service name.

    Pointing either of these at `internal_url` sends the user to a dead host mid-flow,
    and it fails in the browser rather than anywhere a server log would show it.
    """
    kc = _split()
    assert kc.authorization_endpoint().startswith(PUBLIC)
    assert kc.account_link_endpoint("google").startswith(PUBLIC)


def test_the_issuer_never_follows_the_internal_host() -> None:
    """The one that must not move, and the reason it gets its own test.

    `issuer` is not fetched — it is compared against the `iss` claim Keycloak signed, and
    Keycloak builds that claim from its own `KC_HOSTNAME` (the public name). Deriving it
    from `internal_url` would reject **every token** with nothing but an "invalid issuer"
    to go on, which is precisely the failure R-81(3) hit from the other direction.
    """
    assert _split().issuer == f"{PUBLIC}/realms/corpus"
    assert INTERNAL not in _split().issuer


def test_the_two_issuers_differ_only_in_host() -> None:
    """Same realm, same path — the split is about *where*, never about *what*."""
    kc = _split()
    assert kc.issuer.removeprefix(PUBLIC) == kc.internal_issuer.removeprefix(INTERNAL)


# --- shape ---------------------------------------------------------------------------


def test_a_trailing_slash_does_not_produce_a_double_slash() -> None:
    kc = KeycloakSettings(server_url=PUBLIC + "/", internal_url=INTERNAL + "/", realm="corpus")
    assert "//realms" not in kc.issuer.removeprefix("https://")
    assert "//realms" not in kc.internal_issuer.removeprefix("http://")
    assert "//admin" not in kc.admin_url.removeprefix("http://")


def test_the_admin_api_is_not_under_the_realm_path() -> None:
    """Pre-existing invariant, re-pinned now that admin_url has moved host.

    The Admin REST API lives at /admin/realms/<realm>, not under the issuer's
    /realms/<realm> — deriving it from an issuer is a 404 trap.
    """
    kc = _split()
    assert kc.admin_url == f"{INTERNAL}/admin/realms/corpus"
    assert "/realms/corpus/admin" not in kc.admin_url
