"""Auth tests (T-103) — token validation, role gate, and the /auth routes.

Route tests stub Keycloak's token/admin endpoints with `respx` and mint RS256
tokens with the test keypair (see conftest). The ASGI test client uses a
different httpx transport, so respx only intercepts the app's outbound Keycloak
calls, never the in-process requests to our own routes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth.dependencies import require_admin
from app.auth.principal import Principal
from app.auth.roles import Role
from app.auth.tokens import TokenValidationError, validate_access_token
from app.config import get_settings
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.usefixtures("patch_jwks")


# ---- Token validation --------------------------------------------------------


async def test_validate_access_token_ok(make_token: Callable[..., str]) -> None:
    sub = uuid.uuid4()
    principal = await validate_access_token(make_token(sub=sub, roles=("admin", "user")))
    assert principal.sub == sub
    assert principal.is_administrator
    assert Role.NON_ADMINISTRATOR in principal.roles


async def test_validate_rejects_bad_signature(make_token: Callable[..., str]) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(TokenValidationError):
        await validate_access_token(make_token(signing_key=other))


async def test_validate_rejects_wrong_issuer(make_token: Callable[..., str]) -> None:
    with pytest.raises(TokenValidationError):
        await validate_access_token(make_token(iss="http://evil/realms/other"))


async def test_validate_rejects_wrong_azp(make_token: Callable[..., str]) -> None:
    with pytest.raises(TokenValidationError):
        await validate_access_token(make_token(azp="another-client"))


async def test_validate_rejects_expired(make_token: Callable[..., str]) -> None:
    with pytest.raises(TokenValidationError):
        await validate_access_token(make_token(exp=1, iat=1))


# ---- Role dependency ---------------------------------------------------------


def _principal(*roles: Role) -> Principal:
    return Principal(
        sub=uuid.uuid4(),
        email="p@corpus.local",
        display_name=None,
        roles=frozenset(roles),
        azp="corpus-backend",
        claims={},
    )


async def test_require_admin_allows_admin() -> None:
    principal = _principal(Role.ADMINISTRATOR, Role.NON_ADMINISTRATOR)
    assert await require_admin(principal=principal, _user=None) is principal  # type: ignore[arg-type]


async def test_require_admin_rejects_non_admin() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_admin(principal=_principal(Role.NON_ADMINISTRATOR), _user=None)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


# ---- /auth/login -------------------------------------------------------------


async def test_login_ok(
    client: httpx.AsyncClient, session, make_token: Callable[..., str], respx_mock
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    access = make_token(sub=sub, email="admin@corpus.test")
    respx_mock.post(kc.token_endpoint).respond(
        json={
            "access_token": access,
            "refresh_token": "r1",
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "admin@corpus.test", "password": "pw"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == access
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 300
    # R-72(1)/FR-AUT-07: the refresh token reaches the browser ONLY as an httpOnly cookie.
    # A body copy would make the cookie decorative, so its absence is the requirement.
    assert "refresh_token" not in body
    cookie_name = get_settings().session.refresh_cookie_name
    assert resp.cookies[cookie_name] == "r1"
    # The local user row is upserted (keyed to the Keycloak sub) and committed.
    assert await UserRepository(session).get(sub) is not None


async def test_login_cookie_flags(
    client: httpx.AsyncClient, make_token: Callable[..., str], respx_mock
) -> None:
    """The flags ARE the control. Read off the raw header, because `resp.cookies` discards
    every attribute and would pass just as happily on a plain, script-readable cookie."""
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        json={
            "access_token": make_token(sub=uuid.uuid4(), email="admin@corpus.test"),
            "refresh_token": "r1",
            "expires_in": 300,
            "refresh_expires_in": 1800,
            "token_type": "Bearer",
        }
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "admin@corpus.test", "password": "pw"}
    )
    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=strict" in raw
    assert "Path=/api/v1/auth" in raw
    # The cookie expires with the credential it carries — Keycloak's own `refresh_expires_in`,
    # which on the shipped realm is `ssoSessionIdleTimeout` (R-72(2)).
    assert "Max-Age=1800" in raw


async def test_login_cookie_without_refresh_expiry_is_a_session_cookie(
    client: httpx.AsyncClient, make_token: Callable[..., str], respx_mock
) -> None:
    """No `refresh_expires_in` → no `Max-Age`, rather than a number we invented."""
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        json={
            "access_token": make_token(sub=uuid.uuid4(), email="admin@corpus.test"),
            "refresh_token": "r1",
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "admin@corpus.test", "password": "pw"}
    )
    assert "Max-Age" not in resp.headers["set-cookie"]


async def test_login_invalid_credentials(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        400, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "x@corpus.local", "password": "bad"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password."


async def test_login_locked_returns_429(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        400,
        json={"error": "invalid_grant", "error_description": "Account temporarily disabled"},
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "x@corpus.local", "password": "bad"}
    )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "Too many attempts — try again later."


# ---- /auth/refresh & /auth/logout -------------------------------------------


def _cookie_name() -> str:
    return get_settings().session.refresh_cookie_name


def _with_cookie(client: httpx.AsyncClient, value: str) -> httpx.AsyncClient:
    """Set the refresh cookie on the client rather than per request — httpx deprecates the
    per-request form, and the jar is what a browser actually has."""
    client.cookies.set(_cookie_name(), value)
    return client


async def test_refresh_reads_the_cookie_and_rotates_it(
    client: httpx.AsyncClient, respx_mock
) -> None:
    kc = get_settings().keycloak
    route = respx_mock.post(kc.token_endpoint).respond(
        json={
            "access_token": "a2",
            "refresh_token": "r2",
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    resp = await _with_cookie(client, "r1").post("/api/v1/auth/refresh")
    assert resp.status_code == 200
    # The cookie, not a body, is what was presented upstream.
    assert "refresh_token=r1" in route.calls.last.request.content.decode()
    assert resp.json()["access_token"] == "a2"
    assert "refresh_token" not in resp.json()
    # Rotated: the response replaces the cookie, so an active client's cookie lifetime
    # tracks the realm's idle timeout rather than the time of login.
    assert resp.cookies[_cookie_name()] == "r2"


async def test_refresh_without_a_cookie_is_401(client: httpx.AsyncClient) -> None:
    """No cookie is the ordinary first-visit case, and it must not reach Keycloak at all —
    `respx_mock` is absent here, so any upstream call would fail the test."""
    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired session."


async def test_refresh_invalid_clears_the_cookie(client: httpx.AsyncClient, respx_mock) -> None:
    """A spent cookie must be taken away on the same response that rejects it.

    THE POINT OF THIS TEST is that the handler *raises*, and FastAPI discards the injected
    `Response` when it does — so a `response.delete_cookie()` written before the `raise` never
    reaches the browser, and every later request would re-present a token that can only 401.
    """
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(400, json={"error": "invalid_grant"})
    resp = await _with_cookie(client, "stale").post("/api/v1/auth/refresh")
    assert resp.status_code == 401
    raw = resp.headers["set-cookie"]
    assert raw.startswith(f"{_cookie_name()}=")
    assert "Max-Age=0" in raw or "expires=Thu, 01 Jan 1970" in raw.lower()


async def test_logout_revokes_and_clears(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    route = respx_mock.post(kc.logout_endpoint).respond(204)
    resp = await _with_cookie(client, "r1").post("/api/v1/auth/logout")
    assert resp.status_code == 204
    assert "refresh_token=r1" in route.calls.last.request.content.decode()
    assert "Max-Age=0" in resp.headers["set-cookie"]


async def test_logout_without_a_cookie_is_204_and_calls_nothing(
    client: httpx.AsyncClient,
) -> None:
    """Idempotent. No `respx_mock`, so an upstream call would fail rather than pass quietly."""
    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 204


async def test_logout_clears_the_cookie_even_when_keycloak_is_down(
    client: httpx.AsyncClient, respx_mock
) -> None:
    """FR-AUT-08: a user who clicked Sign out must end up signed out locally regardless.

    The alternative — reporting 503 while leaving a live session cookie in the jar — is the
    worst of both, and it is the shape you get for free if the clear is written onto the
    injected `Response` instead of onto the exception.
    """
    kc = get_settings().keycloak
    respx_mock.post(kc.logout_endpoint).respond(502)
    resp = await _with_cookie(client, "r1").post("/api/v1/auth/logout")
    assert resp.status_code == 503
    assert "Max-Age=0" in resp.headers["set-cookie"]


# ---- /auth/me ----------------------------------------------------------------


async def test_me_ok(client: httpx.AsyncClient, session, make_token: Callable[..., str]) -> None:
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=sub, email="u@corpus.local", display_name="U"
    )
    token = make_token(sub=sub, email="u@corpus.local", roles=("user",))
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(sub)
    assert body["email"] == "u@corpus.local"
    assert body["roles"] == ["user"]
    assert body["is_active"] is True


async def test_me_inactive_user_403(
    client: httpx.AsyncClient, session, make_token: Callable[..., str]
) -> None:
    sub = uuid.uuid4()
    repo = UserRepository(session)
    user = await repo.upsert_from_claims(sub=sub, email="d@corpus.local")
    await repo.set_active(user, is_active=False)
    token = make_token(sub=sub, email="d@corpus.local")
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_me_requires_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


# ---- /auth/change-password ---------------------------------------------------


async def test_change_password_ok(
    client: httpx.AsyncClient, session, make_token: Callable[..., str], respx_mock
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    token = make_token(sub=sub, email="u@corpus.local", roles=("user",))
    # Both the current-password verify and the client_credentials grant hit the
    # token endpoint; a single stub returning a token satisfies both.
    respx_mock.post(kc.token_endpoint).respond(
        json={"access_token": "svc", "refresh_token": "r", "expires_in": 60, "token_type": "Bearer"}
    )
    reset = respx_mock.put(f"{kc.admin_url}/users/{sub}/reset-password").respond(204)

    resp = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "old", "new_password": "new"},
    )
    assert resp.status_code == 204
    assert reset.called


async def test_change_password_reports_a_forbidden_reset_as_a_server_fault(
    client: httpx.AsyncClient, session, make_token: Callable[..., str], respx_mock
) -> None:
    """T-110: change-password is the second route that reaches the Admin API.

    `reset-password` needs the service account's `manage-users` role, so it can hit exactly the
    under-provisioned condition the /users routes hit — and the user's own credentials are
    perfectly fine, which is why this must not be a 401/403 (blaming them) or a 503 (promising
    it will pass).
    """
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.test")
    token = make_token(sub=sub, email="u@corpus.test", roles=("user",))
    respx_mock.post(kc.token_endpoint).respond(
        json={"access_token": "svc", "refresh_token": "r", "expires_in": 60, "token_type": "Bearer"}
    )
    respx_mock.put(f"{kc.admin_url}/users/{sub}/reset-password").respond(403)

    resp = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "old", "new_password": "new"},
    )
    assert resp.status_code == 500
    assert "unavailable" not in resp.text.lower(), "a missing role is not an outage"


async def test_change_password_wrong_current_401(
    client: httpx.AsyncClient, session, make_token: Callable[..., str], respx_mock
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    token = make_token(sub=sub, email="u@corpus.local", roles=("user",))
    respx_mock.post(kc.token_endpoint).respond(
        400, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )
    resp = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "wrong", "new_password": "new"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Current password is incorrect."
