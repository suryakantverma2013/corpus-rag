"""Cloud-account linking — the FR-AUT-11 browser flow (T-214, R-63).

Corpus stores **no third-party credential** (R-63(1)): Keycloak runs the Google OAuth
exchange, keeps the tokens (`storeToken`) and refreshes them, and the backend reads the live
access token from the broker endpoint. So this module holds no token, no table and no
provider secret — it builds two URLs, verifies what comes back, and grants one role.

**Why linking is two legs, measured rather than assumed.** Keycloak's client-initiated
account-linking endpoint requires a browser SSO session; with none it redirects straight back
carrying ``link_error=not_logged_in`` (probed live, T-214). ROPC issues tokens and creates no
browser session (R-28), so there is never one when the user clicks "Add from cloud drive".
Hence:

    leg 1  authorization code flow on `corpus-linking`  → the browser is now authenticated
    leg 2  /broker/google/link with a session-bound hash → Google consent → back

Leg 1 is also what makes the flow *safe*, not merely possible. Whoever types credentials into
Keycloak's page is the account Google gets linked to — so the callback exchanges the code and
checks the authenticated ``sub`` against the Corpus user who started the flow. Without that
check a user could start linking and an entirely different account could receive the link,
while Corpus reported success to the initiator.

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


def complete_url(provider: CloudProvider, settings: Settings) -> str:
    """Where Keycloak returns after leg 2 (the link itself)."""
    base = settings.cloud.callback_base_url.rstrip("/")
    return f"{base}/api/v1/cloud/links/{provider.value}/complete"


def start_link(*, sub: uuid.UUID, provider: CloudProvider, settings: Settings) -> str:
    """Leg 1's URL: authenticate this browser against Keycloak (FR-AUT-11).

    `prompt=login` is deliberate. Without it a browser that happens to hold a Keycloak SSO
    session would sail through, and the flow would link Google to *that* session's user —
    which is correct only by coincidence. Forcing the prompt makes the identity check below
    a check rather than a formality, and FR-AUT-11 already accepts one password prompt here.
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
            "code_challenge": _challenge(_verifier(payload, settings)),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.keycloak.authorization_endpoint()}?{query}"


def _link_hash(*, nonce: str, session_state: str, client_id: str, provider: str) -> str:
    """Keycloak's account-link hash.

    ``base64url(sha256(nonce + session_state + client_id + provider))``.

    Unpadded, and the concatenation order is Keycloak's, not ours — it is verified server-side,
    so a wrong order shows up as ``link_error=invalid_hash`` rather than as anything subtler.
    """
    raw = f"{nonce}{session_state}{client_id}{provider}".encode()
    return _b64(hashlib.sha256(raw).digest())


async def complete_authentication(
    *,
    code: str,
    raw_state: str,
    kc: KeycloakClient,
    settings: Settings,
) -> str:
    """Finish leg 1 and return leg 2's URL (the provider consent redirect).

    Three things happen here and each one is load-bearing: the state is verified (this is our
    flow, not a forged callback), the code is exchanged (which authenticates the browser and
    yields the `session_state` leg 2 is bound to), and the authenticated `sub` is checked
    against the initiator.
    """
    state = decode_state(raw_state, settings)
    payload = raw_state.partition(".")[0]

    tokens = await kc.exchange_linking_code(
        code=code,
        redirect_uri=callback_url(state.provider, settings),
        verifier=_verifier(payload, settings),
    )
    session_state = tokens.get("session_state")
    if not session_state:
        # Without it the link hash cannot be computed, and Keycloak would answer
        # `invalid_hash` — a message that describes our arithmetic rather than the cause.
        raise LinkError("the linking token response carried no session_state")

    authenticated = _sub_of(tokens)
    if authenticated != state.sub:
        # Not merely unequal — this is the case where a different person authenticated in the
        # browser, and completing would link *their* Google account while telling the
        # initiator it worked.
        log.warning(
            "cloud.link_identity_mismatch",
            expected=str(state.sub),
            authenticated=str(authenticated) if authenticated else None,
        )
        raise LinkIdentityMismatchError("the browser authenticated as a different user")

    link_nonce = secrets.token_urlsafe(16)
    # A fresh state for leg 2 — same signature, new nonce — so the completion endpoint knows
    # whose link it is without trusting anything Keycloak echoes back.
    onward = encode_state(
        _LinkState(
            sub=state.sub,
            provider=state.provider,
            nonce=link_nonce,
            expires_at=int(time.time()) + settings.cloud.link_state_ttl_seconds,
        ),
        settings,
    )
    query = urlencode(
        {
            "client_id": settings.keycloak.linking_client_id,
            "redirect_uri": f"{complete_url(state.provider, settings)}?state={onward}",
            "nonce": link_nonce,
            "hash": _link_hash(
                nonce=link_nonce,
                session_state=session_state,
                client_id=settings.keycloak.linking_client_id,
                provider=state.provider.value,
            ),
        }
    )
    return f"{settings.keycloak.account_link_endpoint(state.provider.value)}?{query}"


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
    "complete_authentication",
    "decode_state",
    "grant_read_token",
    "link_status",
    "return_to",
    "start_link",
    "unlink",
]
