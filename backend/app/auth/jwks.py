"""JWKS signing-key resolution (T-103).

The realm's public signing keys are fetched over **httpx** (not PyJWT's stock
``PyJWKClient``, whose ``urllib`` fetch blocks the event loop and is invisible to
``respx`` in tests). Keys are cached per JWKS URI and re-fetched once when a token
presents an unseen ``kid`` (key rotation). ``get_signing_key`` is the seam tests
monkeypatch to return a static test key.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import httpx
import jwt

from app.config import get_settings

_JWKS_TIMEOUT = 10.0


class JwksError(Exception):
    """Raised when the signing key for a token cannot be resolved."""


class _JwksResolver:
    """Caches realm signing keys by ``kid``, refetching on a miss."""

    def __init__(self, jwks_uri: str) -> None:
        self._jwks_uri = jwks_uri
        self._keys: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> Any:
        key = self._keys.get(kid)
        if key is not None:
            return key
        async with self._lock:
            if kid not in self._keys:
                await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise JwksError(f"no signing key for kid={kid!r}")
        return key

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=_JWKS_TIMEOUT) as client:
                resp = await client.get(self._jwks_uri)
                resp.raise_for_status()
            jwk_set = jwt.PyJWKSet.from_dict(resp.json())
        except (httpx.HTTPError, jwt.PyJWKError) as exc:
            raise JwksError(f"failed to fetch JWKS: {exc}") from exc
        self._keys = {jwk.key_id: jwk.key for jwk in jwk_set.keys if jwk.key_id}


@lru_cache
def _resolver(jwks_uri: str) -> _JwksResolver:
    """Process-wide resolver per URI (survives fresh ``create_app()`` in tests)."""
    return _JwksResolver(jwks_uri)


async def get_signing_key(token: str) -> Any:
    """Return the public signing key for ``token`` from the realm JWKS.

    Overridable seam — token-validation tests monkeypatch this to return a static
    test public key so they never hit the network.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise JwksError(f"malformed token header: {exc}") from exc
    if not kid:
        raise JwksError("token header has no 'kid'")
    return await _resolver(get_settings().keycloak.jwks_uri).get_key(kid)
