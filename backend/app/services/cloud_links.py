"""Cloud-account linking — the FR-AUT-11 browser flow (T-214, R-63).

Corpus stores **no third-party credential** (R-63(1)): Keycloak runs the Google OAuth
exchange, keeps the tokens (`storeToken`) and refreshes them, and the backend reads the live
access token from the broker endpoint. So this module holds no token, no table and no
provider secret — it builds two URLs, verifies what comes back, and grants one role.

**One request, not two (R-99).** Linking runs as Keycloak's *application-initiated action*:
an ordinary authorization request carrying ``kc_action=idp_link:<alias>``, which both
authenticates the browser and starts the link. It replaced the client-initiated
account-linking endpoint, which Keycloak deprecated and announced for removal — that endpoint
logged its own deprecation on every call, and T-725 had already had to rename a parameter to
survive one Keycloak bump.

    one  authorization code flow on `corpus-linking` with `kc_action` → login → Keycloak's
         "do you want to link?" confirmation → provider consent → back here

**What makes it safe is unchanged, and is now structurally tighter.** Whoever types
credentials into Keycloak's page is the account the provider gets linked to — so the callback
exchanges the code and checks the authenticated ``sub`` against the Corpus user who started
the flow. Without that check a user could start linking and an entirely different account
could receive the link, while Corpus reported success to the initiator. Under the old two-leg
flow that check had to *bridge* two separate requests; here authentication and linking are one
request, so there is no interval between the identity verified and the identity linked.

Two things this depends on, both measured on Keycloak 26.4 before the migration (R-99(2)):
``prompt=login`` composes with ``kc_action`` (without it an existing SSO session would decide
silently who gets linked), and Keycloak relays our ``state`` verbatim through the provider and
back, which is what keeps the flow stateless.

**There is no pending-link table and no in-process store**, which is a deliberate constraint
rather than a shortcut. R-63 promised no schema change; and in-process state is the failure
R-43(1) rejected for the processing lock — with more than one API worker, leg 1 and the
callback can land on different processes, and the flow would fail for a reason no log
explains. So the state travels in the OAuth ``state`` parameter, signed with HMAC-SHA256.

**The PKCE verifier is derived, never carried.** `corpus-linking` requires S256, and putting
the verifier in the signed state would publish it to browser history and access logs. Instead
the verifier is ``HMAC(secret, "pkce:" + payload)``: reproducible at the callback from the
state we just verified, and uncomputable by anyone without the signing key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlencode

import structlog

from app.auth.keycloak_client import KeycloakClient, KeycloakError
from app.config import Settings

log = structlog.get_logger(__name__)


class CloudProvider(StrEnum):
    """Providers a user may link.

    One member, and NFR-CMP-02 says exactly that: v1 commits to Google Drive, and the
    *mechanism* is what is provider-agnostic — Keycloak brokers the token, so a second
    provider is a realm identity provider plus a file-listing adapter. Modelling it as an
    enum keeps the provider a validated path segment rather than free text reaching a URL,
    which is the R-63(6)(1) discipline applied one level up from the file id.
    """

    GOOGLE = "google"


class LinkError(Exception):
    """A linking flow that cannot be completed. Never carries provider or token detail."""


class LinkStateError(LinkError):
    """The `state` was absent, malformed, forged, or expired.

    One class for all four on purpose: they are indistinguishable to an honest client (whose
    browser simply took too long) and telling them apart only helps someone probing the
    signature.
    """


class LinkIdentityMismatchError(LinkError):
    """Leg 1 authenticated a different Keycloak user than the one who started the flow."""


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """What `GET /cloud/links/{provider}` reports."""

    provider: CloudProvider
    linked: bool
    #: The provider-side account, when linked — a Google address the GUI can show so the
    #: user knows *which* account they are importing from. Never a token, never an id.
    account: str | None = None


@dataclass(frozen=True, slots=True)
class _LinkState:
    """The half-finished flow, as it travels through the browser."""

    sub: uuid.UUID
    provider: CloudProvider
    nonce: str
    expires_at: int


def _key(settings: Settings) -> bytes:
    """The signing key.

    `KEYCLOAK_CLIENT_SECRET` rather than a new setting: it is already required for the
    backend to authenticate at all, it is already secret, and adding a second one would give
    operators a way to leave this signature keyless without noticing.
    """
    return settings.keycloak.client_secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str, settings: Settings) -> str:
    return _b64(hmac.new(_key(settings), payload.encode("ascii"), hashlib.sha256).digest())


def encode_state(state: _LinkState, settings: Settings) -> str:
    payload = _b64(
        json.dumps(
            {
                "sub": str(state.sub),
                "p": state.provider.value,
                "n": state.nonce,
                "exp": state.expires_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    return f"{payload}.{_sign(payload, settings)}"


def decode_state(raw: str, settings: Settings) -> _LinkState:
    """Verify and unpack. Raises :class:`LinkStateError` for anything that is not ours."""
    payload, _, signature = raw.partition(".")
    if not payload or not signature:
        raise LinkStateError("malformed state")
    # `compare_digest`, not `==`: this is the check an attacker would time.
    if not hmac.compare_digest(signature, _sign(payload, settings)):
        raise LinkStateError("bad signature")
    try:
        data = json.loads(_unb64(payload))
        state = _LinkState(
            sub=uuid.UUID(data["sub"]),
            provider=CloudProvider(data["p"]),
            nonce=data["n"],
            expires_at=int(data["exp"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        # A valid signature over an unreadable payload means our own encoder changed shape,
        # not an attack — but it is still an unusable flow, so it fails the same way.
        raise LinkStateError("unreadable state") from exc
    if state.expires_at < int(time.time()):
        raise LinkStateError("expired state")
    return state


def _verifier(payload: str, settings: Settings) -> str:
    """The PKCE verifier for this flow, derived from the state rather than stored with it.

    Base64url of an HMAC is 43 characters of the unreserved set, which is exactly RFC 7636's
    minimum length and needs no further encoding.
    """
    raw = hmac.new(_key(settings), f"pkce:{payload}".encode("ascii"), hashlib.sha256)
    return _b64(raw.digest())


def _challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def callback_url(provider: CloudProvider, settings: Settings) -> str:
    """Where Keycloak returns after leg 1. Must match a `corpus-linking` redirect URI."""
    base = settings.cloud.callback_base_url.rstrip("/")
    return f"{base}/api/v1/cloud/links/{provider.value}/callback"


def start_link(*, sub: uuid.UUID, provider: CloudProvider, settings: Settings) -> str:
    """The linking URL: one request that authenticates *and* links (FR-AUT-11, R-99).

    `prompt=login` is deliberate. Without it a browser that happens to hold a Keycloak SSO
    session would sail through, and the flow would link Google to *that* session's user —
    which is correct only by coincidence. Forcing the prompt makes the identity check below
    a check rather than a formality, and FR-AUT-11 already accepts one password prompt here.
    Measured: it composes with `kc_action` rather than being ignored beside it.

    `kc_action` is what replaced the deprecated client-initiated endpoint. The value is
    `idp_link:<alias>`, and the alias is the provider's Keycloak alias — the same string the
    broker token endpoint uses, so there is one name for one provider and no mapping table.
    """
    state = _LinkState(
        sub=sub,
        provider=provider,
        nonce=secrets.token_urlsafe(16),
        expires_at=int(time.time()) + settings.cloud.link_state_ttl_seconds,
    )
    encoded = encode_state(state, settings)
    payload = encoded.partition(".")[0]
    query = urlencode(
        {
            "client_id": settings.keycloak.linking_client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": callback_url(provider, settings),
            "state": encoded,
            "prompt": "login",
            "kc_action": f"idp_link:{provider.value}",
            "code_challenge": _challenge(_verifier(payload, settings)),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.keycloak.authorization_endpoint()}?{query}"


async def complete_link(
    *,
    code: str,
    raw_state: str,
    kc: KeycloakClient,
    settings: Settings,
) -> uuid.UUID:
    """Finish the linking request and return the user it belongs to (R-99).

    Under the AIA action the link has **already happened** by the time Keycloak sends the
    browser back here: one authorization request authenticated the user, ran the
    `idp_link` action, took them to the provider and returned. So this is no longer a
    hand-off to a second leg - it is the end of the flow, and all that remains is to prove
    whose link it is.

    Two things are load-bearing and neither is new:

    * **The state is verified before anything else.** It is ours, signed, and carries the
      `sub` of whoever started the flow. A forged callback fails here.
    * **The authenticated `sub` is checked against that initiator.** This is R-63's
      guarantee: whoever typed credentials into Keycloak's page is the account the
      provider was linked to. Without the check, one user could start linking and a
      different account could receive the link while Corpus reported success to the first.

    The caller grants `read-token` and reads the link back; that is deliberately not done
    here, so this function has one job and the route keeps the ordering visible.
    """
    state = decode_state(raw_state, settings)
    payload = raw_state.partition(".")[0]

    tokens = await kc.exchange_linking_code(
        code=code,
        redirect_uri=callback_url(state.provider, settings),
        verifier=_verifier(payload, settings),
    )

    authenticated = _sub_of(tokens)
    if authenticated != state.sub:
        # Not merely unequal - this is the case where a different person authenticated in
        # the browser, and reporting success would tell the initiator that *their* account
        # was linked when somebody else's was.
        log.warning(
            "cloud.link_identity_mismatch",
            expected=str(state.sub),
            authenticated=str(authenticated) if authenticated else None,
        )
        raise LinkIdentityMismatchError("the browser authenticated as a different user")

    return state.sub


def _sub_of(tokens: dict) -> uuid.UUID | None:
    """The authenticated subject, read from the id token's payload.

    Unverified decode, and safe here for a specific reason: this token came back over TLS
    from a direct server-to-server call to Keycloak's own token endpoint in exchange for a
    code we minted the challenge for. There is no untrusted party in that path. Verifying the
    signature would be re-proving the transport, and `validate_access_token` is for tokens
    that arrive from a client.
    """
    raw = tokens.get("id_token") or ""
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
        return uuid.UUID(str(claims.get("sub")))
    except ValueError, TypeError:
        return None


async def grant_read_token(*, sub: uuid.UUID, kc: KeycloakClient, settings: Settings) -> None:
    """Grant the `broker` client's `read-token` role, without which the link is inert.

    **This is the step that has to exist, and finding that out cost a live round trip.** The
    realm sets `addReadTokenRoleOnCreate: true`, which reads as "granted at link time" and is
    not: it fires only when brokering *creates* an account, and the provider is
    `linkOnly: true` precisely to forbid that — so the two settings are mutually exclusive in
    effect. After a completely successful link the user held no `broker` role at all and
    `broker_token()` raised `AccountNotLinkedError`, naming the wrong cause, because Keycloak
    answers 403 both for "not linked" and for "missing read-token".

    The realm-default-roles alternative was tried and fails the import outright: realm roles
    are created *before* Keycloak's built-in clients exist, so a composite referencing
    `broker` cannot resolve. Hence a per-user grant, here, once the link is real.

    `read-token` is safe to hold unconditionally — it permits reading *your own* brokered
    token and yields nothing without a federated link.
    """
    admin_token = await kc.service_account_token()
    broker_uuid = await kc.admin_get_client_uuid(client_id="broker", admin_token=admin_token)
    role = await kc.admin_get_client_role(
        client_uuid=broker_uuid, role_name="read-token", admin_token=admin_token
    )
    await kc.admin_add_client_roles(
        sub=str(sub),
        client_uuid=broker_uuid,
        roles=[{"id": role["id"], "name": role["name"]}],
        admin_token=admin_token,
    )


async def link_status(
    *, sub: uuid.UUID, provider: CloudProvider, kc: KeycloakClient, settings: Settings
) -> LinkStatus:
    """Whether this user has linked `provider` (FR-AUT-11).

    Read from the federated-identity list rather than by attempting a broker call: the broker
    endpoint cannot distinguish "not linked" from "missing read-token", and a status surface
    that reports *unlinked* for a linked account would send the user round the consent flow
    to fix something consent does not fix.
    """
    admin_token = await kc.service_account_token()
    identities = await kc.admin_list_federated_identities(sub=str(sub), admin_token=admin_token)
    for identity in identities:
        if identity.get("identityProvider") == provider.value:
            return LinkStatus(
                provider=provider, linked=True, account=identity.get("userName") or None
            )
    return LinkStatus(provider=provider, linked=False)


async def unlink(
    *, sub: uuid.UUID, provider: CloudProvider, kc: KeycloakClient, settings: Settings
) -> None:
    """Remove the link (FR-AUT-11). Documents already imported are untouched — they are copies.

    The `read-token` role is deliberately **not** revoked. It confers nothing without a
    federated identity, and revoking it would make a subsequent re-link silently inert unless
    the grant ran again — trading a harmless role for the exact defect this task already fixed
    once.
    """
    admin_token = await kc.service_account_token()
    await kc.admin_remove_federated_identity(
        sub=str(sub), alias=provider.value, admin_token=admin_token
    )


def return_to(settings: Settings, **params: str) -> str:
    """The GUI URL the browser is sent back to, with the outcome in the query string.

    Built from `CLOUD_RETURN_URL` and never from the request: a redirect target a caller can
    choose is an open redirect, and this endpoint is reachable without a bearer token.
    """
    base = settings.cloud.return_url
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(params)}"


__all__ = [
    "CloudProvider",
    "KeycloakError",
    "LinkError",
    "LinkIdentityMismatchError",
    "LinkStateError",
    "LinkStatus",
    "complete_link",
    "decode_state",
    "grant_read_token",
    "link_status",
    "return_to",
    "start_link",
    "unlink",
]
