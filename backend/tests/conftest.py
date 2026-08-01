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

import asyncio
import os
import pathlib
import selectors
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator

# Force the rate limiter (T-105) onto in-memory storage *before* any app import so
# the suite never needs a live Redis. `app.security.rate_limit` reads this at import
# time via the lru_cached settings; setting it here guarantees the limiter is built
# with `memory://`. `setdefault` lets an explicit env override win.
os.environ.setdefault("RATELIMIT_STORAGE_URI", "memory://")

# Same reasoning, one failure later (T-302). FastAPI resolves `JobQueueDep` *before* the
# handler body, so any route carrying it builds and module-globally caches a real arq Redis
# pool — even for a request that is about to be refused, and even in a test that never
# reaches the enqueue. That pool is bound to the creating test's event loop, so the failure
# surfaces in `test_job_queue`'s own teardown ("Event loop is closed") hundreds of tests
# later, in a different file. Selecting the null backend here makes the accident impossible
# rather than relying on every future test module remembering to override the dependency.
# The arq path is still covered: `test_job_queue` constructs `ArqJobQueue` directly.
os.environ.setdefault("QUEUE_BACKEND", "none")

# Third instance of the same lesson (T-304). `route` reaches for the process-wide chat client
# whenever `RAGContext.chat` is not injected, so any future graph test that forgets the
# injection would build a real `AsyncOpenAI` — caching a loop-bound httpx pool, and, if a key
# happens to be present in the environment, billing a classification per test run. The
# deterministic backend makes that accident impossible; the OpenAI path is still covered,
# because `test_llm.py` constructs `OpenAIChatClient` directly.
os.environ.setdefault("LLM_BACKEND", "fake")

# Fourth instance (T-305), and the one with a bill attached: `retrieve` reaches for the
# process-wide embedding client whenever `RAGContext.embeddings` is not injected, and unlike
# `route` it fails **closed** — so a forgotten injection would not merely bill an embedding
# per test run, it would turn a missing key into a failed turn and a red suite that looks
# like a retrieval regression. `test_embeddings.py` builds `OpenAIEmbeddingClient` directly,
# so the real path keeps its coverage.
os.environ.setdefault("EMBEDDING_BACKEND", "fake")

# The live-test gates read `os.environ`, so `.env` has to reach it explicitly (T-309).
#
# `pydantic-settings` reads `.env` into `Settings` and **never** into `os.environ`, so a gate
# spelled `os.environ.get("KEYCLOAK_LIVE_ADMIN_PASSWORD")` sees nothing from that file. This
# worked anyway for one release only because **`deepeval`'s pytest plugin was loading `.env`
# as a side effect** — so disabling that plugin (pyproject `-p no:deepeval`) silently stopped
# six live auth tests from running, while they still reported as "skipped" rather than as a
# problem. Whether a live test executes must not depend on which vendor plugins happen to be
# installed; this makes it depend on the file the developer actually edits.
#
# `setdefault`, and placed **after** the backend selections above, so a real environment
# variable still wins and nothing here can override the fake backends those lines pin.
_ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.is_file():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

# Keep every module's logger observable by `structlog.testing.capture_logs` (T-302).
# `cache_logger_on_first_use=True` latches a module-level `structlog.get_logger(__name__)`
# onto the processor chain configured at its first use, and nothing un-latches it — so a
# log assertion silently sees nothing. This *must* be an environment decision rather than a
# fixture: `app.main` builds the app at import time (`app = create_app()`, the uvicorn
# entrypoint), so the latch closes during the first `from app...` import, long before any
# fixture runs. Three T-302 telemetry tests passed alone and failed in the full suite on
# exactly that ordering. The processor chain is unchanged, so the suite still exercises
# production's.
os.environ.setdefault("LOG_CACHE_LOGGERS", "false")

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


def pytest_asyncio_loop_factories(config, item):  # noqa: ANN001, ANN201, ARG001
    """Pin a selector event loop on Windows (T-301, FR-PER-01).

    The LangGraph checkpointer is psycopg-backed, and psycopg's async driver waits on
    sockets with ``loop.add_reader``. Windows' default `ProactorEventLoop` does not
    implement it, so *every* psycopg connection raises
    ``InterfaceError: Psycopg cannot use the 'ProactorEventLoop'``. Verified on this box:
    under a selector loop psycopg and asyncpg both work, side by side, in one loop.

    This changes the loop for the whole suite on win32, which is why T-301 re-ran the
    full suite when it landed. Linux/uvloop is untouched.

    Return **exactly one** factory: pytest-asyncio parametrizes every async test over the
    mapping it gets back, and hides the parameter only when there is a single entry.
    Returning ``None`` or an empty mapping is a `UsageError`.
    """
    if sys.platform == "win32":
        return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
    return {"default": asyncio.EventLoop}


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
        email: str = "admin@corpus.test",
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


@pytest.fixture(autouse=True)
async def _reset_graph_and_checkpointer() -> AsyncIterator[None]:
    """Drop the process-global graph and checkpointer after every test (T-301).

    Both are module globals rather than `lru_cache`d values because
    `AsyncPostgresSaver.__init__` captures ``asyncio.get_running_loop()``. pytest-asyncio
    gives each test its own loop, so a provider leaking out of one test hands the *next*
    one a psycopg pool bound to a loop that is already closed — which surfaces as an
    unrelated failure somewhere downstream. Teardown-only: nothing needs building up front.
    """
    yield
    from app.rag.graph import close_graph
    from app.services.checkpointer import close_checkpointer
    from app.services.llm import close_chat_client

    await close_graph()
    await close_checkpointer()
    # Same loop-affinity argument, one client further along (T-304): the SDK pools inside a
    # client bound to the creating loop.
    await close_chat_client()


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
