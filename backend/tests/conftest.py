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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncConnection,
    AsyncSession,
    create_async_engine,
)
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
async def db_connection() -> AsyncIterator[AsyncConnection]:
    """One connection in an outer transaction, rolled back on teardown.

    Split out of `session` so a test can open **more than one** session on the same
    transaction — which the T-210 SSE stream needs, since it opens a short-lived session
    per poll tick (R-41(7)) and must still see the rows the test seeded.
    """
    engine = create_async_engine(get_settings().database.url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, DBAPIError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable for repository tests: {exc}")

    txn = await conn.begin()
    try:
        yield conn
    finally:
        if txn.is_active:
            await txn.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
async def session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    db_session = AsyncSession(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield db_session
    finally:
        await db_session.close()


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


@pytest.fixture(autouse=True)
def _reset_stream_registry() -> Iterator[None]:
    """Isolate the per-user SSE stream counter between tests (T-210, R-41(7)).

    The registry is process-global, and a route test that never drains its stream leaves
    its slot held — which would surface as an unrelated later test getting a `429`.
    """
    from app.services.document_events import registry

    registry.reset()
    yield
    registry.reset()


@pytest.fixture
def app(session: AsyncSession, db_connection: AsyncConnection):  # noqa: ANN201 (FastAPI app)
    """App instance with the DB session overridden to the transactional test session."""
    from app.db.session import get_session, get_stream_sessionmaker
    from app.main import create_app

    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    def _stream_sessionmaker() -> AsyncSession:
        # A *real* short-lived session per poll tick (R-41(7)), bound to the test's
        # connection so it joins the transaction the fixture rolls back. Not the shared
        # `session` object: reusing it would hand the stream a populated identity map,
        # and the engine would then read stale attribute values instead of the SELECT's —
        # exactly the coupling `_read_states` expunges to avoid.
        return AsyncSession(
            bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    application.dependency_overrides[get_session] = _override_session
    application.dependency_overrides[get_stream_sessionmaker] = lambda: _stream_sessionmaker
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ANN001
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
