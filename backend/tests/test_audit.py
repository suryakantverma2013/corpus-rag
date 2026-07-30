"""Audit-log tests (T-107, NFR-SEC-08) — hooks + the admin read endpoint.

Auth/user flows are driven through the real routes (Keycloak stubbed with ``respx``,
RS256 tokens minted with the test keypair — see conftest). The service commits inside
the transactional test session, so the written rows are queryable on the same
``session`` and rolled back on teardown. ``GET /api/v1/audit`` is exercised for admin
access, filtering, and the non-admin 403 gate.

**Every assertion here is scoped to the actor the test minted — never to a global count**
(T-109). `audit_logs` is append-only and nothing truncates it between runs, so
``len(list_events(event_type=X)) == 1`` is only true on an empty database. Any row
committed outside the suite's rollback fixture breaks it: a live ingestion smoke writes an
FR-ING-05/R-31 malware-purge row, and every one of these tests then fails in a way that
reads like a real regression rather than pollution (T-212 hit exactly this). Each test uses
a fresh random ``sub``, so ``actor_id=`` makes the count deterministic regardless of what
else is in the table. The one null-actor case cannot be filtered that way and narrows on
its own details payload instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.enums import AuditEventType
from app.db.repositories.audit_log import AuditLogRepository
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
    admin_sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=admin_sub, email="admin@corpus.test", display_name="Admin"
    )
    token = make_token(sub=admin_sub, email="admin@corpus.test", roles=("admin", "user"))
    return admin_sub, {"Authorization": f"Bearer {token}"}


# ---- Auth hooks --------------------------------------------------------------


async def test_login_success_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    access = make_token(sub=sub, email="u@corpus.local")
    respx_mock.post(kc.token_endpoint).respond(
        json={"access_token": access, "refresh_token": "r1", "expires_in": 300}
    )

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "u@corpus.local", "password": "pw"}
    )
    assert resp.status_code == 200

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.AUTH, actor_id=sub
    )
    assert len(rows) == 1
    assert rows[0].actor_id == sub
    assert rows[0].details["action"] == "login"


async def test_failed_login_audited_with_null_actor(
    client: httpx.AsyncClient, session: AsyncSession, respx_mock
) -> None:
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        400, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "x@corpus.local", "password": "bad"}
    )
    assert resp.status_code == 401

    # The one row no actor filter can reach, so it is narrowed by its own payload instead.
    listed = await AuditLogRepository(session).list_events(event_type=AuditEventType.AUTH)
    rows = [
        row
        for row in listed
        if row.actor_id is None and row.details.get("email") == "x@corpus.local"
    ]
    assert len(rows) == 1
    assert rows[0].details["action"] == "login_failed"


async def test_refresh_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    # The subject logged in earlier, so their local row exists (audit FK → users.id).
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    new_access = make_token(sub=sub, email="u@corpus.local")
    respx_mock.post(kc.token_endpoint).respond(
        json={"access_token": new_access, "refresh_token": "r2", "expires_in": 300}
    )

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "r1"})
    assert resp.status_code == 200

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.AUTH, actor_id=sub
    )
    assert len(rows) == 1
    assert rows[0].details["action"] == "refresh"
    assert rows[0].actor_id == sub


async def test_logout_audited_with_subject(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    refresh_jwt = make_token(sub=sub, email="u@corpus.local")
    respx_mock.post(kc.logout_endpoint).respond(204)

    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_jwt})
    assert resp.status_code == 204

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.AUTH, actor_id=sub
    )
    assert len(rows) == 1
    assert rows[0].details["action"] == "logout"
    # Subject recovered from the (unverified) refresh-token decode.
    assert rows[0].actor_id == sub


async def test_change_password_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    token = make_token(sub=sub, email="u@corpus.local", roles=("user",))
    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.put(f"{kc.admin_url}/users/{sub}/reset-password").respond(204)

    resp = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "old", "new_password": "new"},
    )
    assert resp.status_code == 204

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.AUTH, actor_id=sub
    )
    assert len(rows) == 1
    assert rows[0].details["action"] == "password_change"
    assert rows[0].actor_id == sub


# ---- User-management hooks ---------------------------------------------------


async def test_create_user_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    admin_sub, headers = await _admin_headers(session, make_token)
    new_sub = uuid.uuid4()
    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.post(f"{kc.admin_url}/users").respond(
        201, headers={"Location": f"{kc.admin_url}/users/{new_sub}"}
    )

    resp = await client.post(
        "/api/v1/users",
        headers=headers,
        json={"email": "new@corpus.local", "password": "pw"},
    )
    assert resp.status_code == 201

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.USER_ROLE_CHANGE, actor_id=admin_sub
    )
    assert len(rows) == 1
    assert rows[0].actor_id == admin_sub
    assert rows[0].target_id == str(new_sub)
    assert rows[0].details["action"] == "create"


async def test_promote_writes_permission_change(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    admin_sub, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()
    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.get(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(
        json=[{"name": "user"}]
    )
    respx_mock.get(f"{kc.admin_url}/roles/admin").respond(
        json={"id": str(uuid.uuid4()), "name": "admin"}
    )
    respx_mock.post(f"{kc.admin_url}/users/{target}/role-mappings/realm").respond(204)
    respx_mock.get(f"{kc.admin_url}/users/{target}").respond(
        json={"id": str(target), "email": "t@corpus.local", "enabled": True}
    )

    resp = await client.patch(f"/api/v1/users/{target}", headers=headers, json={"role": "admin"})
    assert resp.status_code == 200

    perm = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.PERMISSION_CHANGE, actor_id=admin_sub
    )
    assert len(perm) == 1
    assert perm[0].actor_id == admin_sub
    assert perm[0].target_id == str(target)
    assert perm[0].details == {"granted": ["admin"]}


async def test_delete_user_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    respx_mock,
) -> None:
    kc = get_settings().keycloak
    admin_sub, headers = await _admin_headers(session, make_token)
    target = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=target, email="t@corpus.local")
    respx_mock.post(kc.token_endpoint).respond(json=_SVC_TOKEN)
    respx_mock.delete(f"{kc.admin_url}/users/{target}").respond(204)

    resp = await client.delete(f"/api/v1/users/{target}", headers=headers)
    assert resp.status_code == 204

    rows = await AuditLogRepository(session).list_events(
        event_type=AuditEventType.USER_ROLE_CHANGE, actor_id=admin_sub
    )
    assert len(rows) == 1
    assert rows[0].details["action"] == "delete"
    assert rows[0].target_id == str(target)


# ---- Read endpoint -----------------------------------------------------------


async def test_get_audit_returns_and_filters(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    admin_sub, headers = await _admin_headers(session, make_token)
    actor = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=actor, email="actor@corpus.local")
    repo = AuditLogRepository(session)
    await repo.record(event_type=AuditEventType.AUTH, actor_id=actor, details={"action": "login"})
    await repo.record(
        event_type=AuditEventType.USER_ROLE_CHANGE,
        actor_id=admin_sub,
        target_id=str(actor),
        details={"action": "create"},
    )
    await session.commit()

    # Assertions are scoped to the two actors this test minted, never to the whole table.
    # `audit_logs` is append-only with no per-test cleanup, so a global count is only ever
    # right on an empty database — and anything that commits an audit row outside the
    # suite's rollback fixture (a live ingestion smoke, for instance) makes it fail in a
    # way that reads like a genuine regression.
    resp = await client.get("/api/v1/audit", headers=headers)
    assert resp.status_code == 200
    mine = [row for row in resp.json() if row["actor_id"] in {str(actor), str(admin_sub)}]
    assert len(mine) == 2

    resp = await client.get(
        "/api/v1/audit", headers=headers, params={"event_type": "AUTH", "actor_id": str(actor)}
    )
    body = resp.json()
    assert len(body) == 1
    assert body[0]["event_type"] == "AUTH"
    assert body[0]["actor_id"] == str(actor)

    resp = await client.get("/api/v1/audit", headers=headers, params={"actor_id": str(admin_sub)})
    assert len(resp.json()) == 1


async def test_get_audit_forbidden_for_non_admin(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email="u@corpus.local")
    headers = {"Authorization": f"Bearer {make_token(sub=sub, roles=('user',))}"}
    resp = await client.get("/api/v1/audit", headers=headers)
    assert resp.status_code == 403


async def test_get_audit_requires_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/audit")).status_code == 401
