"""Live verification of the two T-602 claims a mocked suite cannot make (NFR-SEC-01/07).

**Skipped unless `KEYCLOAK_LIVE_ADMIN_PASSWORD` is set**, on `tests/test_auth_live.py`'s
convention: these need a running Keycloak with the `corpus` realm imported, they **mutate**
that realm, and the rate-limit half needs a reachable Redis.

Two things belong here and nothing else does.

**One: the revocation asymmetry, measured rather than asserted.** The offline module mints its
own tokens, so it can only ever demonstrate that `require_admin` reads whatever claim it is
handed. It cannot show what happens when a *real* administrator is demoted in a *real* realm —
and that is the whole of R-77(1). Here the role is removed through the Admin API and the
holder's existing access token is presented again: it still works. Then the session is
refreshed and the new token does not. That sequence is the evidence for the ruling, and it is
not reproducible with a test keypair.

**Two: the limiter against real Redis.** Every other rate-limit test runs on `memory://`
(conftest pins it before the app imports), so the production storage path — a sync `redis://`
URI called from an async route, and `swallow_errors` when it goes away — is exercised nowhere.
R-77(3)'s fix is about bucket *keys*, and keys are exactly what changes between storage
backends, so verifying it on the storage the product actually uses is the point.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.main import create_app
from tests.security import nfr

ADMIN_EMAIL = "admin@corpus.local"


def _live_password() -> str:
    password = os.environ.get("KEYCLOAK_LIVE_ADMIN_PASSWORD", "")
    if not password:
        pytest.skip("KEYCLOAK_LIVE_ADMIN_PASSWORD is unset; live security tests skipped")
    return password


async def _require_realm() -> None:
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
        pytest.skip(f"realm not imported ({response.status_code})")


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test() -> AsyncIterator[None]:
    """Dispose the process-wide engine around each test — the loop-affinity hazard T-110 records.

    These use the **real** `get_session`, so they touch the `lru_cache`d engine directly rather
    than the transactional fixtures, and pytest-asyncio gives every test its own loop.
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
    """The real app, committing for real. `https://` because the refresh cookie is `Secure`."""
    await _require_realm()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://live", timeout=30.0
    ) as client:
        yield client


# --- R-77(1): the two revocation latencies, against a real realm ---------------------------


@nfr("NFR-SEC-01")
async def test_a_demoted_administrator_keeps_the_role_until_the_token_is_replaced(
    api: httpx.AsyncClient,
) -> None:
    """R-77(1), measured end to end — the reason the ruling names a window rather than an instant.

    A real administrator is created in the realm, logs in, is demoted through the Admin API,
    and then presents **the token they already hold**. It still works, because the role lives
    in that token and nothing consults the realm again. Refreshing the session mints a token
    without the role and the same request is refused.

    This is the sequence an operator needs to understand, and it cannot be shown offline: a
    minted-token test proves only that the check reads a claim, never that the *authority*
    behind that claim has no way to reach an issued token.
    """
    password = _live_password()
    admin_login = await api.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": password}
    )
    assert admin_login.status_code == 200, admin_login.text
    root = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    email = f"t602-{uuid.uuid4().hex[:10]}@corpus.local"
    created = await api.post(
        "/api/v1/users",
        headers=root,
        json={
            "email": email,
            "display_name": "T602 Live Subject",
            "password": "Str0ng-Passw0rd!",
            "role": "admin",
        },
    )
    assert created.status_code == 201, created.text
    subject_id = created.json()["id"]

    try:
        session = await api.post(
            "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
        )
        assert session.status_code == 200, session.text
        held = {"Authorization": f"Bearer {session.json()['access_token']}"}

        assert (await api.get("/api/v1/users", headers=held)).status_code == 200, (
            "the new administrator could not use an administrator route"
        )

        demoted = await api.patch(
            f"/api/v1/users/{subject_id}", headers=root, json={"role": "user"}
        )
        assert demoted.status_code == 200, demoted.text

        # The token in hand is unchanged, and so is what it can do.
        still_admin = await api.get("/api/v1/users", headers=held)
        assert still_admin.status_code == 200, (
            "the demotion took effect on an already-issued token — if this ever fails, "
            "something now consults a live source for the role and R-77(1)'s window no "
            "longer exists, which is a *better* world but a different one than the ruling "
            "describes"
        )

        # Refreshing is what actually applies it: Keycloak mints the new claim set.
        refreshed = await api.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 200, refreshed.text
        renewed = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}

        assert (await api.get("/api/v1/users", headers=renewed)).status_code == 403, (
            "the refreshed token still carried the administrator role"
        )
    finally:
        await api.delete(f"/api/v1/users/{subject_id}", headers=root)


@nfr("NFR-SEC-01")
async def test_disabling_a_real_account_takes_effect_on_the_next_request(
    api: httpx.AsyncClient,
) -> None:
    """The other half, and the operational lever R-77(1) names: disable, do not demote.

    Unlike the role, `is_active` is mirrored into the local `users` row and read on every
    request, so this takes effect immediately against a live realm and a live token — no
    refresh, no waiting for expiry.
    """
    password = _live_password()
    admin_login = await api.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": password}
    )
    assert admin_login.status_code == 200, admin_login.text
    root = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    email = f"t602-{uuid.uuid4().hex[:10]}@corpus.local"
    created = await api.post(
        "/api/v1/users",
        headers=root,
        json={
            "email": email,
            "display_name": "T602 Live Subject",
            "password": "Str0ng-Passw0rd!",
            "role": "admin",
        },
    )
    assert created.status_code == 201, created.text
    subject_id = created.json()["id"]

    try:
        session = await api.post(
            "/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd!"}
        )
        assert session.status_code == 200, session.text
        held = {"Authorization": f"Bearer {session.json()['access_token']}"}
        assert (await api.get("/api/v1/users", headers=held)).status_code == 200

        disabled = await api.patch(
            f"/api/v1/users/{subject_id}", headers=root, json={"is_active": False}
        )
        assert disabled.status_code == 200, disabled.text

        refused = await api.get("/api/v1/users", headers=held)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "User account is disabled", (
            "the refusal came from the role gate rather than from `is_active` — the lever an "
            "operator would reach for does not work the way R-77(1) says"
        )
    finally:
        await api.delete(f"/api/v1/users/{subject_id}", headers=root)


# --- R-77(3): the limiter on the storage the product actually uses -------------------------


@pytest.fixture
def live_redis_limiter() -> AsyncIterator[None]:  # noqa: PT004
    """Rebuild the process-global limiter on the configured Redis, and restore it afterwards.

    Every other rate-limit test runs on `memory://` because `conftest` pins it before the app
    imports (so the suite never needs a broker). That is right for the suite and means the
    production storage path is otherwise untested — and bucket **keys**, which is what R-77(3)
    is about, are precisely what a storage swap can change.
    """
    import limits.storage

    from app.security import rate_limit as module

    uri = os.environ.get("RATELIMIT_LIVE_STORAGE_URI", "redis://localhost:6379/1")
    try:
        storage = limits.storage.storage_from_string(uri)
        if not storage.check():
            pytest.skip(f"Redis not reachable at {uri}")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis not reachable at {uri}: {exc}")

    original = module.limiter._storage
    module.limiter._storage = storage
    try:
        yield
    finally:
        module.limiter._storage = original


@nfr("NFR-SEC-07")
@pytest.mark.usefixtures("live_redis_limiter")
async def test_the_upload_budget_is_per_user_on_real_redis(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-77(3) on the production storage backend, with a different id on every call.

    The offline test proves the shared scope works against in-memory storage. This proves the
    bucket key is what we think it is once a real `limits` Redis backend is composing it —
    which is the layer that turned out to be doing something other than we assumed.
    """
    password = _live_password()
    login = await api.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": password})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # A unique limit string per run, so this never collides with a previous run's window in a
    # store that (unlike `memory://`) outlives the process.
    monkeypatch.setattr(get_settings().ratelimit, "upload", "2/minute")
    monkeypatch.setattr(
        get_settings().ratelimit, "storage_uri", os.environ.get("RATELIMIT_LIVE_STORAGE_URI", "")
    )
    from app.security.rate_limit import limiter

    limiter.reset()

    codes = [
        (await api.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)).status_code
        for _ in range(3)
    ]

    assert codes[2] == 429, (
        f"three deletes with three different ids on real Redis produced {codes}; the budget "
        "is still keyed on the concrete path"
    )
