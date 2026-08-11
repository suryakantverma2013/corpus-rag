"""Admin Users API tests (T-104) — authz matrix + Keycloak Admin-API proxying.

Routes are gated by ``require_admin``; the caller's local ``users`` row is seeded
so the gate resolves to a role check (403) rather than a missing-user (401).
Keycloak's token + Admin REST endpoints are stubbed with ``respx`` off
``get_settings().keycloak`` URLs; captured routes assert the backend actually
proxied. The ASGI transport handles our own routes, so respx only intercepts the
app's outbound Keycloak calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.usefixtures("patch_jwks")

_SVC_TOKEN = {
    "access_token": "svc",
    "refresh_token": "r",
    "expires_in": 60,
    "token_type": "Bearer",
}


async def _admin_headers(
    session: AsyncSession, make_token: Callable[..., str]
) -> tuple[uuid.UUID, dict[str, str]]:
    """Seed an admin caller's local row and return (sub, auth headers)."""
    admin_sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=admin_sub, email="admin@corpus.test", display_name="Admin"
    )
    token = make_token(sub=admin_sub, email="admin@corpus.test", roles=("admin", "user"))
    return admin_sub, {"Authorization": f"Bearer {token}"}


def _rep(
    sub: uuid.UUID, *, email: str, first: str = "", last: str = "", enabled: bool = True
) -> dict[str, object]:
    return {
        "id": str(sub),
        "email": email,
        "firstName": first,
        "lastName": last,
        "enabled": enabled,
    }


# ---- Authz matrix (FR-USR-07) ------------------------------------------------


async def test_non_admin_forbidden_on_every_route(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    headers = {"Authorization": f"Bearer {make_token(sub=sub, roles=('user',))}"}
    target = uuid.uuid4()

    assert (await client.get("/api/v1/users", headers=headers)).status_code == 403
    assert (
        await client.post("/api/v1/users", headers=headers, json={"email": "n@x", "password": "p"})
    ).status_code == 403
    assert (
        await client.patch(f"/api/v1/users/{target}", headers=headers, json={"is_active": False})
    ).status_code == 403
    assert (await client.delete(f"/api/v1/users/{target}", headers=headers)).status_code == 403


async def test_routes_require_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/users")).status_code == 401
    assert (
        await client.post("/api/v1/users", json={"email": "n@x", "password": "p"})
    ).status_code == 401


# ---- Create ------------------------------------------------------------------


async def test_create_user_default_role(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    new_sub = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    create = respx_mock.post(f"{kc.admin_url}/users").respond(
        201, headers={"Location": f"{kc.admin_url}/users/{new_sub}"}
    )

    resp = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "new@corpus.local", "display_name": "New User", "password": "pw"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(new_sub)
    assert body["email"] == "new@corpus.local"
    assert body["roles"] == ["user"]
    assert body["is_active"] is True
    assert create.called
    # Local mirror upserted under the Keycloak-generated sub.
    assert await UserRepository(session).get(new_sub) is not None


async def test_create_admin_assigns_role(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    new_sub = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.post(f"{kc.admin_url}/users").respond(
        201, headers={"Location": f"{kc.admin_url}/users/{new_sub}"}
    )
    respx_mock.get(f"{kc.admin_url}/roles/admin").respond(
        json={"id": str(uuid.uuid4()), "name": "admin"}
    )
    add_role = respx_mock.post(f"{kc.admin_url}/users/{new_sub}/role-mappings/realm").respond(204)

    resp = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "boss@corpus.local", "password": "pw", "role": "admin"},
    )

    assert resp.status_code == 201
    assert resp.json()["roles"] == ["admin", "user"]
    assert add_role.called


async def test_create_conflict_409(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.post(f"{kc.admin_url}/users").respond(
        409, json={"errorMessage": "User exists with same email"}
    )

    resp = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "dup@corpus.local", "password": "pw"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A user with that email already exists."


# ---- List --------------------------------------------------------------------


async def test_list_users_maps_roles(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    admin_id, user_id = uuid.uuid4(), uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users").respond(
        json=[
            _rep(admin_id, email="a@corpus.local", first="A", last="One"),
            _rep(user_id, email="b@corpus.local", enabled=False),
        ]
    )
    respx_mock.get(f"{kc.admin_url}/roles/admin/users").respond(json=[{"id": str(admin_id)}])

    resp = await client.get("/api/v1/users", headers=headers)
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()}
    assert rows[str(admin_id)]["roles"] == ["admin", "user"]
    assert rows[str(admin_id)]["display_name"] == "A One"
    assert rows[str(user_id)]["roles"] == ["user"]
    assert rows[str(user_id)]["is_active"] is False


# ---- Patch -------------------------------------------------------------------


async def test_patch_disable_and_rename(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(
        json=[{"name": "user"}]
    )
    update = respx_mock.put(f"{kc.admin_url}/users/{target}").respond(204)
    respx_mock.get(f"{kc.admin_url}/users/{target}").respond(
        json=_rep(target, email="t@corpus.local", first="New", last="Name", enabled=False)
    )

    resp = await client.patch(
        f"/api/v1/users/{target}",
        headers=headers,
        json={"display_name": "New Name", "is_active": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "New Name"
    assert body["is_active"] is False
    assert body["roles"] == ["user"]
    assert update.called
    # Mirror reflects the disable (immediate-deactivation enforcement).
    assert (await UserRepository(session).get(target)).is_active is False


async def test_patch_promote_to_admin(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(
        json=[{"name": "user"}]
    )
    respx_mock.get(f"{kc.admin_url}/roles/admin").respond(
        json={"id": str(uuid.uuid4()), "name": "admin"}
    )
    add_role = respx_mock.post(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(204)
    respx_mock.get(f"{kc.admin_url}/users/{target}").respond(
        json=_rep(target, email="t@corpus.local")
    )

    resp = await client.patch(f"/api/v1/users/{target}", headers=headers, json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["roles"] == ["admin", "user"]
    assert add_role.called


async def test_patch_reset_password(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(
        json=[{"name": "user"}]
    )
    reset = respx_mock.put(f"{kc.admin_url}/users/{target}/reset-password").respond(204)
    respx_mock.get(f"{kc.admin_url}/users/{target}").respond(
        json=_rep(target, email="t@corpus.local")
    )

    resp = await client.patch(
        f"/api/v1/users/{target}", headers=headers, json={"new_password": "fresh"}
    )
    assert resp.status_code == 200
    assert reset.called


async def test_patch_empty_body_422(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _admin_headers(session, make_token)
    resp = await client.patch(f"/api/v1/users/{uuid.uuid4()}", headers=headers, json={})
    assert resp.status_code == 422


async def test_patch_self_demote_blocked(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    admin_sub, headers = await _admin_headers(session, make_token)
    resp = await client.patch(f"/api/v1/users/{admin_sub}", headers=headers, json={"role": "user"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "You cannot perform this action on your own account."


async def test_patch_self_disable_blocked(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Disabling is the other half of demotion — an inactive admin cannot administer either."""
    admin_sub, headers = await _admin_headers(session, make_token)
    resp = await client.patch(
        f"/api/v1/users/{admin_sub}", headers=headers, json={"is_active": False}
    )
    assert resp.status_code == 409


async def test_an_administrator_cannot_remove_their_own_administration(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """OI-25 / R-62(2) — the admin-lockout invariant, pinned as behaviour.

    These routes are administrator-gated, and all three ways to strip an administrator —
    demote, disable, delete — are refused when the target is the **actor**. The acting
    administrator therefore always survives their own request, so no *sequential* series of
    calls can take the system to zero administrators. That, not a `count(admins) > 1` check,
    is what closes FR-USR-03/07's lockout hole: a count is read-then-act and races with
    itself, and roles live in Keycloak (R-28) so it could not be made transactional anyway.

    **Accepted residual (R-62(2)):** two administrators deleting *each other* concurrently
    both pass this check and both succeed. A count guard would not catch that either. It is
    recoverable — the Keycloak admin console is independent of these application roles.

    The three separate tests above assert the codes; this one asserts they hold *together*,
    which is the property the requirement actually needs.
    """
    admin_sub, headers = await _admin_headers(session, make_token)

    demote = await client.patch(
        f"/api/v1/users/{admin_sub}", headers=headers, json={"role": "user"}
    )
    disable = await client.patch(
        f"/api/v1/users/{admin_sub}", headers=headers, json={"is_active": False}
    )
    delete = await client.delete(f"/api/v1/users/{admin_sub}", headers=headers)

    assert [demote.status_code, disable.status_code, delete.status_code] == [409, 409, 409]


async def test_patch_not_found_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(404)

    resp = await client.patch(
        f"/api/v1/users/{target}", headers=headers, json={"display_name": "X"}
    )
    assert resp.status_code == 404


# ---- Delete ------------------------------------------------------------------


async def test_delete_user(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=target, email="t@corpus.local")

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    delete = respx_mock.delete(f"{kc.admin_url}/users/{target}").respond(204)

    resp = await client.delete(f"/api/v1/users/{target}", headers=headers)
    assert resp.status_code == 204
    assert delete.called
    # Local row retained (FK integrity) but disabled.
    assert (await UserRepository(session).get(target)).is_active is False


async def test_delete_self_blocked(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    admin_sub, headers = await _admin_headers(session, make_token)
    resp = await client.delete(f"/api/v1/users/{admin_sub}", headers=headers)
    assert resp.status_code == 409


# ---- Upstream error mapping --------------------------------------------------


async def test_delete_upstream_503(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.delete(f"{kc.admin_url}/users/{target}").respond(500)

    resp = await client.delete(f"/api/v1/users/{target}", headers=headers)
    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        # Our own service account is short a realm-management role, or its token was
        # rejected: the *server* is misconfigured, so 500 — never 503 (which promises the
        # condition clears on its own) and never 403 (which would blame the caller, who is
        # an authenticated admin).
        (403, 500),
        (401, 500),
        # Keycloak rejected a request we built — our bug, equally un-retryable.
        (400, 500),
        (422, 500),
        # Genuinely transient: upstream failure or an unenumerated status.
        (500, 503),
        (502, 503),
        (418, 503),
    ],
)
async def test_admin_api_status_codes_map_to_the_right_class(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
    upstream: int,
    expected: int,
) -> None:
    """T-110: the catch-all used to file **every** unexpected status as 503.

    That is what made an under-provisioned service account read as an outage — `POST /users`
    succeeded while `GET /users` "failed", a combination no real outage produces. The split
    matters beyond diagnosis: `503` tells every proxy and retrying client "try again later",
    and a missing role never heals on retry.
    """
    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users").respond(upstream)

    resp = await client.get("/api/v1/users", headers=headers)
    assert resp.status_code == expected, f"{upstream} upstream should map to {expected}"


async def test_a_forbidden_admin_call_is_logged_with_its_status_and_path(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    """The log line is part of the fix, not decoration.

    The response body deliberately says nothing about roles or paths — the caller cannot act
    on either — so this event is the *only* place the cause is recorded. Its absence is what
    made the original T-110 diagnosis take three probes.
    """
    import structlog

    kc = get_settings().keycloak
    _, headers = await _admin_headers(session, make_token)

    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users").respond(403, json={"error": "HTTP 403 Forbidden"})

    with structlog.testing.capture_logs() as logs:
        resp = await client.get("/api/v1/users", headers=headers)

    assert resp.status_code == 500
    failures = [entry for entry in logs if entry["event"] == "keycloak.admin_call_failed"]
    assert len(failures) == 1
    assert failures[0]["status_code"] == 403
    assert failures[0]["path"] == "/users"
    assert failures[0]["method"] == "GET"
    # And the caller is told nothing that only an operator can use.
    assert "403" not in resp.text
    assert "role" not in resp.text.lower()
