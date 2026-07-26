"""Shared test fixtures (T-102, extended for auth T-103).

`session` gives each test an `AsyncSession` wrapped in a transaction that is rolled
back on teardown — isolated, no pollution of the local `corpus` DB (already migrated
to head by T-101). If Postgres is unreachable, DB-backed tests skip rather than error.

The auth fixtures (`rsa_keys`, `make_token`, `patch_jwks`, `app`, `client`) let route
and token-validation tests run without a live Keycloak: RS256 tokens are minted with a
test keypair and `app.auth.jwks.get_signing_key` is monkeypatched to return the test
public key. Keycloak HTTP calls (token/admin endpoints) are stubbed with `respx`.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator

# Force the rate limiter (T-105) onto in-memory storage *before* any app import so
# the suite never needs a live Redis. `app.security.rate_limit` reads this at import
# time via the lru_cached settings; setting it here guarantees the limiter is built
# with `memory://`. `setdefault` lets an explicit env override win.
os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")

import httpx  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    """Isolate rate-limit buckets between tests (T-105).

    The ASGI test transport always presents one client IP, so without a reset the
    per-IP counters would accumulate across tests and trip the limit in unrelated
    auth tests. Also restores ``enabled`` in case a test toggled it.
    """
    from app.security.rate_limit import limiter

    limiter.enabled = True
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database.url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, DBAPIError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable for repository tests: {exc}")

    txn = await conn.begin()
    db_session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield db_session
    finally:
        await db_session.close()
        if txn.is_active:
            await txn.rollback()
        await conn.close()
        await engine.dispose()


# ---- Auth fixtures (T-103) ---------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """A test RSA keypair standing in for the Keycloak realm signing key."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture
def make_token(rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> Callable[..., str]:
    """Return a factory that mints RS256 access tokens with valid default claims."""
    private, _ = rsa_keys
    kc = get_settings().keycloak

    def _make(
        *,
        sub: uuid.UUID | None = None,
        email: str = "admin@corpus.local",
        roles: tuple[str, ...] = ("admin", "user"),
        signing_key: rsa.RSAPrivateKey | None = None,
        **overrides: object,
    ) -> str:
        now = int(time.time())
        claims: dict[str, object] = {
            "iss": kc.issuer,
            "sub": str(sub or uuid.uuid4()),
            "aud": kc.client_id,
            "azp": kc.client_id,
            "exp": now + 300,
            "iat": now,
            "email": email,
            "realm_access": {"roles": list(roles)},
        }
        claims.update(overrides)
        return jwt.encode(
            claims, signing_key or private, algorithm="RS256", headers={"kid": "test"}
        )

    return _make


@pytest.fixture
def patch_jwks(
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey], monkeypatch: pytest.MonkeyPatch
) -> rsa.RSAPublicKey:
    """Bypass the network JWKS fetch: return the test public key for any token."""
    _, public = rsa_keys

    async def _get_signing_key(_token: str) -> rsa.RSAPublicKey:
        return public

    monkeypatch.setattr("app.auth.jwks.get_signing_key", _get_signing_key)
    return public


@pytest.fixture
def app(session: AsyncSession):  # noqa: ANN201 (FastAPI app)
    """App instance with the DB session overridden to the transactional test session."""
    from app.db.session import get_session
    from app.main import create_app

    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = _override_session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ANN001
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
