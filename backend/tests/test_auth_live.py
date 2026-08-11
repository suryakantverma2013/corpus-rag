"""Live auth verification against a real Keycloak realm (T-110; R-28, T-103/T-104).

**Skipped unless `KEYCLOAK_LIVE_ADMIN_PASSWORD` is set**, on the same convention as the
live-OpenAI tests in `test_embeddings.py`/`test_llm.py`: it needs a running Keycloak with the
`corpus` realm imported (`deployment/keycloak/README.md`) and it *mutates* that realm, so it
must never run by accident.

**Why it exists.** Every other auth test mints its own RSA keypair and mocks the JWKS fetch,
which is right for a unit test and means the whole of R-28's integration was unexercised until
2026-07-30. The first live run found two defects that no mocked test could reach:

1. the service account was granted only `manage-users`, so `GET /api/v1/users` failed — a 403
   from the Admin API surfacing as `503 "Authentication service unavailable."`;
2. Keycloak's declarative user profile requires `firstName`/`lastName` and `VERIFY_PROFILE` is
   enabled, so a user created with just an email **could never log in** ("Account is not fully
   set up") — because under backend-mediated ROPC there is no browser in which to satisfy a
   required action. Two of the three realistic `display_name` inputs produced a dead account.

Both are fixed in `deployment/keycloak/corpus-realm.json`; these tests are what stops them
coming back. `test_realm_artifact_imports_and_works` is the important one — it imports the
committed artifact into a throwaway realm, so it fails if the *file* regresses even when this
developer's hand-fixed realm still works.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.auth.roles import Role, extract_roles
from app.auth.tokens import validate_access_token
from app.config import get_settings
from app.db.repositories.users import UserRepository
from app.db.session import get_engine, get_sessionmaker
from app.main import create_app

ADMIN_EMAIL = "admin@corpus.local"
REALM_FILE = (
    pathlib.Path(__file__).resolve().parents[2] / "deployment" / "keycloak" / "corpus-realm.json"
)


def _live_password() -> str:
    password = os.environ.get("KEYCLOAK_LIVE_ADMIN_PASSWORD", "")
    if not password:
        pytest.skip("KEYCLOAK_LIVE_ADMIN_PASSWORD is unset; live Keycloak tests skipped")
    return password


async def _require_realm() -> None:
    """Skip rather than fail when the realm is absent — an un-imported realm is a setup gap."""
    settings = get_settings().keycloak
    url = (
        f"{settings.server_url.rstrip('/')}/realms/{settings.realm}"
        "/.well-known/openid-configuration"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Keycloak unreachable at {url}: {exc}")
    if response.status_code != 200:  # pragma: no cover - environment dependent
        pytest.skip(
            f"realm not imported ({response.status_code}); see deployment/keycloak/README.md"
        )


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test() -> AsyncIterator[None]:
    """Dispose the process-wide engine around every test in this file.

    These tests deliberately use the **real** `get_session` (so logins actually commit), which
    means they touch the `@lru_cache`d engine directly rather than the transactional
    `session`/`db_connection` fixtures. `get_engine` caches an asyncpg pool bound to the loop
    that created it, and pytest-asyncio gives each test a fresh loop — so without this the
    second DB-touching test in the file dies with "attached to a different loop" / "Event loop
    is closed". Same loop-affinity hazard `_reset_graph_and_checkpointer` handles in `conftest`
    for the checkpointer pool.
    """
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    """The real app — **no `get_session` override**, so every request commits for real."""
    await _require_realm()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://live", timeout=30.0
    ) as client:
        yield client


@pytest.fixture
async def admin_token(api: httpx.AsyncClient) -> str:
    response = await api.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": _live_password()}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


# --- the token path -----------------------------------------------------------


async def test_a_realm_signed_token_validates_against_the_real_jwks(admin_token: str) -> None:
    """The one thing the mocked suite cannot check: our validation against Keycloak's own keys.

    Includes the two assertions T-103 added for the ROPC quirk that an access token's `aud` is
    `account` unless an audience mapper is configured — so this also proves the mapper survived
    the realm import.
    """
    principal = await validate_access_token(admin_token)

    assert principal.email == ADMIN_EMAIL
    assert principal.azp == "corpus-backend"
    audience = principal.claims["aud"]
    assert "corpus-backend" in ([audience] if isinstance(audience, str) else audience)
    assert str(principal.claims["iss"]).endswith(f"/realms/{get_settings().keycloak.realm}")
    assert Role.ADMINISTRATOR in extract_roles(principal.claims, client_id="corpus-backend")
    assert principal.is_administrator


async def test_login_mirrors_the_user_locally_keyed_on_sub(
    api: httpx.AsyncClient, admin_token: str
) -> None:
    """R-28: the local `users` row is keyed on the Keycloak `sub`, which is what every FK in
    the schema hangs off — so a mismatch here would corrupt ownership, not just identity."""
    principal = await validate_access_token(admin_token)
    async with get_sessionmaker()() as session:
        row = await UserRepository(session).get_by_email(ADMIN_EMAIL)

    assert row is not None
    assert row.id == principal.sub
    assert row.is_active


async def test_a_junk_token_is_refused(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


# --- the admin surface, against the real Admin API ----------------------------


async def test_listing_users_reads_realm_roles(api: httpx.AsyncClient, admin_token: str) -> None:
    """Regression for defect 1: `manage-users` alone cannot read realm roles, and the 403 was
    mapped to `503 Authentication service unavailable` — an authorization gap reported as an
    outage, the same misattribution class as T-213's botocore leak."""
    response = await api.get("/api/v1/users", headers={"Authorization": f"Bearer {admin_token}"})

    assert response.status_code == 200, response.text
    listed = response.json()
    assert ADMIN_EMAIL in [user["email"] for user in listed]
    admin_row = next(user for user in listed if user["email"] == ADMIN_EMAIL)
    # The roles are what needed `view-realm`; an empty list would mean the enrichment silently
    # degraded rather than failed.
    assert Role.ADMINISTRATOR.value in admin_row["roles"]


async def test_a_user_created_with_only_an_email_can_log_in(
    api: httpx.AsyncClient, admin_token: str
) -> None:
    """Regression for defect 2, and the reason it matters: `display_name` is optional on
    `POST /api/v1/users`, so this is the *default* way an admin creates a user.

    Before the T-110 realm fix this returned 201 and then 401 for ever, with no recovery path
    — ROPC cannot satisfy a required action, and Corpus has no browser login flow.
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    email = f"live-{uuid.uuid4().hex[:8]}@corpus.local"
    password = f"Live-{uuid.uuid4().hex[:12]}"

    created = await api.post(
        "/api/v1/users",
        headers=headers,
        json={"email": email, "password": password},  # no display_name, deliberately
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    try:
        assert created.json()["roles"] == [Role.NON_ADMINISTRATOR.value], "§4.9 default role"

        login = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, f"a nameless user cannot log in: {login.text}"

        # FR-USR-07: the authorization matrix, with a real non-admin token.
        forbidden = await api.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert forbidden.status_code == 403
    finally:
        removed = await api.delete(f"/api/v1/users/{user_id}", headers=headers)
        assert removed.status_code == 204


async def test_change_password_round_trips(api: httpx.AsyncClient, admin_token: str) -> None:
    """The only path that needs the service account's `manage-users` role.

    Restores the original password, so the realm is left exactly as it was found.
    """
    original = _live_password()
    interim = f"Interim-{uuid.uuid4().hex[:12]}"
    headers = {"Authorization": f"Bearer {admin_token}"}

    changed = await api.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": original, "new_password": interim},
    )
    assert changed.status_code == 204, changed.text

    relogin = await api.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": interim})
    assert relogin.status_code == 200
    restored = await api.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {relogin.json()['access_token']}"},
        json={"current_password": interim, "new_password": original},
    )
    assert restored.status_code == 204, "the original password was NOT restored"

    wrong = await api.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "definitely-not-it", "new_password": "Whatever-123456"},
    )
    assert wrong.status_code != 204


async def test_refresh_then_logout_revokes(api: httpx.AsyncClient) -> None:
    login = await api.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": _live_password()}
    )
    assert login.status_code == 200
    refreshed = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refreshed.status_code == 200

    token = refreshed.json()["refresh_token"]
    assert (await api.post("/api/v1/auth/logout", json={"refresh_token": token})).status_code == 204
    assert (
        await api.post("/api/v1/auth/refresh", json={"refresh_token": token})
    ).status_code != 200


# --- the artifact itself ------------------------------------------------------


async def test_realm_artifact_imports_and_works() -> None:
    """Import the **committed** `corpus-realm.json` into a throwaway realm and prove both
    T-110 fixes come from the file.

    This is the test that would have caught either defect. The others exercise *this box's*
    realm, which a hand-fix in the admin console can silently keep working while the artifact
    everyone else imports stays broken.

    Needs master credentials (`KC_ADMIN_USER`/`KC_ADMIN_PASSWORD`) since creating a realm is
    not something the `corpus` service account may do.
    """
    user = os.environ.get("KC_ADMIN_USER", "")
    password = os.environ.get("KC_ADMIN_PASSWORD", "")
    if not (user and password):
        pytest.skip("KC_ADMIN_USER/KC_ADMIN_PASSWORD unset; realm-import test skipped")

    base = get_settings().keycloak.server_url.rstrip("/")
    payload: dict[str, Any] = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    payload["realm"] = f"corpus-verify-{uuid.uuid4().hex[:8]}"
    realm = payload["realm"]

    async with httpx.AsyncClient(timeout=60.0) as http:
        try:
            token_response = await http.post(
                f"{base}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": user,
                    "password": password,
                },
            )
        except httpx.HTTPError as exc:  # pragma: no cover - environment dependent
            # Skip, never fail: this test does not use the `api` fixture (it needs no app), so
            # `_require_realm`'s reachability check does not cover it — and without this the
            # connect error arrives *before* the status check below. Caught for real when
            # Keycloak was stopped mid-session: the suite went red for an absent environment,
            # which is exactly the signal this convention exists to keep honest.
            pytest.skip(f"Keycloak unreachable at {base}: {exc}")
        if token_response.status_code != 200:  # pragma: no cover - environment dependent
            pytest.skip(f"master token failed ({token_response.status_code})")
        adm = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

        created = await http.post(f"{base}/admin/realms", headers=adm, json=payload)
        assert created.status_code in (201, 204), created.text
        try:
            client = (
                await http.get(
                    f"{base}/admin/realms/{realm}/clients",
                    headers=adm,
                    params={"clientId": "corpus-backend"},
                )
            ).json()[0]
            realm_mgmt = (
                await http.get(
                    f"{base}/admin/realms/{realm}/clients",
                    headers=adm,
                    params={"clientId": "realm-management"},
                )
            ).json()[0]
            service_account = (
                await http.get(
                    f"{base}/admin/realms/{realm}/clients/{client['id']}/service-account-user",
                    headers=adm,
                )
            ).json()
            granted = {
                role["name"]
                for role in (
                    await http.get(
                        f"{base}/admin/realms/{realm}/users/{service_account['id']}"
                        f"/role-mappings/clients/{realm_mgmt['id']}",
                        headers=adm,
                    )
                ).json()
            }
            assert {"manage-users", "view-realm", "view-users", "query-users"} <= granted, granted
            # T-214: without `view-clients` the backend cannot resolve the `broker` client, so
            # it cannot grant `read-token` and every completed link is inert. The near-miss is
            # what makes this worth a live assertion: `query-clients` answers the same lookup
            # with `200 []`, which reads as "no such client" rather than "no permission".
            assert "view-clients" in granted, granted

            # T-214, and this one is checked **against a freshly imported realm on purpose**.
            # Keycloak ignores realm-import keys it does not recognise *silently*, so a
            # `clientScopeMappings` block with the wrong shape would import cleanly and leave
            # linking broken — which is exactly how it presented: the account-link endpoint
            # answered `link_error=not_allowed`, naming neither the client nor the role.
            linking = (
                await http.get(
                    f"{base}/admin/realms/{realm}/clients",
                    headers=adm,
                    params={"clientId": "corpus-linking"},
                )
            ).json()[0]
            account = (
                await http.get(
                    f"{base}/admin/realms/{realm}/clients",
                    headers=adm,
                    params={"clientId": "account"},
                )
            ).json()[0]
            scoped = {
                role["name"]
                for role in (
                    await http.get(
                        f"{base}/admin/realms/{realm}/clients/{linking['id']}"
                        f"/scope-mappings/clients/{account['id']}",
                        headers=adm,
                    )
                ).json()
            }
            assert "manage-account-links" in scoped, (
                "corpus-linking has `fullScopeAllowed: false`, so without this scope mapping "
                "Keycloak refuses client-initiated account linking with `not_allowed` (T-214)"
            )

            profile = (
                await http.get(f"{base}/admin/realms/{realm}/users/profile", headers=adm)
            ).json()
            required = {a["name"]: a.get("required") for a in profile["attributes"]}
            assert required.get("firstName") is None, "firstName must not be required (T-110)"
            assert required.get("lastName") is None, "lastName must not be required (T-110)"
            assert required.get("email") is not None, "email should still be required"

            # The behaviour the profile fix exists for.
            email = f"artifact-{uuid.uuid4().hex[:8]}@corpus.local"
            secret = client["secret"]
            probe_password = f"Art-{uuid.uuid4().hex[:12]}"
            made = await http.post(
                f"{base}/admin/realms/{realm}/users",
                headers=adm,
                json={
                    "username": email,
                    "email": email,
                    "emailVerified": True,
                    "enabled": True,
                    "credentials": [
                        {"type": "password", "value": probe_password, "temporary": False}
                    ],
                },
            )
            assert made.status_code == 201, made.text
            login = await http.post(
                f"{base}/realms/{realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "corpus-backend",
                    "client_secret": secret,
                    "username": email,
                    "password": probe_password,
                },
            )
            assert login.status_code == 200, (
                f"nameless user locked out by the artifact: {login.text}"
            )
        finally:
            await http.delete(f"{base}/admin/realms/{realm}", headers=adm)
