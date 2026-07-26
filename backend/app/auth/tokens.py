"""Access-token validation — realm-signed RS256 JWTs (R-28, T-103).

Verifies signature (RS256, algorithm pinned — never trust the token header),
``iss``, ``exp``/``iat``, and ``aud`` against the realm/client, then asserts
``azp == client_id``. The ``azp`` check is the reliable "minted for us" signal
for ROPC access tokens, which default to ``aud: "account"``; the realm export
adds an audience mapper so ``verify_aud`` also passes. Produces a
:class:`~app.auth.principal.Principal` — no database access.
"""

from __future__ import annotations

import uuid
from typing import Any

import jwt

from app.auth import jwks
from app.auth.principal import Principal
from app.auth.roles import extract_roles
from app.config import get_settings

_REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub", "azp"]


class TokenValidationError(Exception):
    """Raised for any invalid/expired/untrusted access token."""


async def validate_access_token(token: str) -> Principal:
    kc = get_settings().keycloak

    try:
        signing_key = await jwks.get_signing_key(token)
    except jwks.JwksError as exc:
        raise TokenValidationError(str(exc)) from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=kc.client_id,
            issuer=kc.issuer,
            leeway=30,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise TokenValidationError(str(exc)) from exc

    if claims.get("azp") != kc.client_id:
        raise TokenValidationError("token 'azp' does not match this client")

    try:
        sub = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenValidationError("token 'sub' is not a UUID") from exc

    email = claims.get("email") or claims.get("preferred_username")
    if not email:
        raise TokenValidationError("token has no email / preferred_username")

    return Principal(
        sub=sub,
        email=email,
        display_name=_display_name(claims),
        roles=frozenset(extract_roles(claims, client_id=kc.client_id)),
        azp=kc.client_id,
        claims=claims,
    )


def _display_name(claims: dict[str, Any]) -> str | None:
    name = claims.get("name")
    if name:
        return name
    parts = [claims.get("given_name"), claims.get("family_name")]
    joined = " ".join(p for p in parts if p)
    return joined or None
