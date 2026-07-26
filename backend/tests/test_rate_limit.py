"""Rate-limiting tests (T-105, NFR-SEC-07).

The limiter runs on in-memory storage here (conftest forces `RATELIMIT_STORAGE_URI=
memory://`), and the autouse `_reset_rate_limiter` fixture clears buckets between
tests. The route-level tests reuse the invalid-credentials login path: slowapi checks
the limit *before* the handler runs, so failed logins still count toward the bucket
and a tripped limit returns 429 without reaching Keycloak.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from starlette.requests import Request

from app.auth.principal import Principal
from app.auth.roles import Role
from app.config import get_settings
from app.security.rate_limit import (
    RATE_LIMITED_COPY,
    client_ip_key,
    limiter,
    principal_or_ip_key,
)

pytestmark = pytest.mark.usefixtures("patch_jwks")


def _login_limit_count() -> int:
    return int(get_settings().ratelimit.login.split("/")[0])


def _stub_invalid_login(respx_mock) -> None:  # noqa: ANN001
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        400, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )


async def _post_login(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": "x@corpus.local", "password": "bad"}
    )


# ---- Route integration -------------------------------------------------------


async def test_login_trips_rate_limit(client: httpx.AsyncClient, session, respx_mock) -> None:
    _stub_invalid_login(respx_mock)
    limit = _login_limit_count()

    # The configured number of attempts are allowed through (all 401 invalid creds).
    for _ in range(limit):
        assert (await _post_login(client)).status_code == 401

    # The next attempt is throttled with the FR-AUT-04 copy.
    resp = await _post_login(client)
    assert resp.status_code == 429
    assert resp.json()["detail"] == RATE_LIMITED_COPY
    assert "Retry-After" in resp.headers


async def test_rate_limit_disabled_never_throttles(
    client: httpx.AsyncClient, session, respx_mock
) -> None:
    _stub_invalid_login(respx_mock)
    limiter.enabled = False  # restored by the autouse fixture

    # Well beyond the configured limit — none are throttled while disabled.
    for _ in range(_login_limit_count() + 3):
        assert (await _post_login(client)).status_code == 401


# ---- Key-function unit tests -------------------------------------------------


def _request(*, client: tuple[str, int] | None, principal: Principal | None) -> Request:
    scope = {"type": "http", "headers": [], "client": client}
    request = Request(scope)
    if principal is not None:
        request.state.principal = principal
    return request


def _principal(sub: uuid.UUID) -> Principal:
    return Principal(
        sub=sub,
        email="p@corpus.local",
        display_name=None,
        roles=frozenset({Role.NON_ADMINISTRATOR}),
        azp="corpus-backend",
        claims={},
    )


def test_client_ip_key_uses_remote_address() -> None:
    request = _request(client=("203.0.113.7", 4444), principal=None)
    assert client_ip_key(request) == "203.0.113.7"


def test_principal_or_ip_key_prefers_principal() -> None:
    sub = uuid.uuid4()
    request = _request(client=("203.0.113.7", 4444), principal=_principal(sub))
    assert principal_or_ip_key(request) == f"user:{sub}"


def test_principal_or_ip_key_falls_back_to_ip() -> None:
    request = _request(client=("203.0.113.7", 4444), principal=None)
    assert principal_or_ip_key(request) == "203.0.113.7"
