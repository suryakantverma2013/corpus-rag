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
    assert body["refresh_token"] == "r1"
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 300
    # The local user row is upserted (keyed to the Keycloak sub) and committed.
    assert await UserRepository(session).get(sub) is not None


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


async def test_refresh_ok(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        json={
            "access_token": "a2",
            "refresh_token": "r2",
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "r1"})
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] == "r2"


async def test_refresh_invalid(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(400, json={"error": "invalid_grant"})
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "stale"})
    assert resp.status_code == 401


async def test_logout_204(client: httpx.AsyncClient, respx_mock) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.logout_endpoint).respond(204)
    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": "r1"})
    assert resp.status_code == 204


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
