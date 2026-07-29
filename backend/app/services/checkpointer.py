"""LangGraph checkpointer seam (T-301, FR-PER-01, R-42).

One `CheckpointerProvider` protocol, two backends: the real `AsyncPostgresSaver` over a
psycopg connection pool, and an in-memory saver for dev and CI.

**Why this lives in `services/` and not `rag/`.** It owns a connection pool bound to the
running event loop and released in the app lifespan — the shape of `object_storage.py`,
`jobs.py` and `embeddings.py`. `app/rag/graph.py` imports it; nothing under `app/rag/`
owns a pool.

**Provisioning is explicit** (R-42(7)). The four `checkpoint*` tables are owned by
langgraph, not Alembic — `app/db/migrations/env.py` already excludes them as "not
app-owned" — and their DDL is created by :func:`ensure_checkpointer_schema`, run once::

    uv run python -m app.services.checkpointer

It is never run lazily on a request path, for three reasons that are not stylistic:
``CREATE INDEX CONCURRENTLY`` (migrations 6-8) cannot run inside a transaction and on a
grown table takes minutes; `setup()` reads `MAX(v)` then inserts into
`checkpoint_migrations`, whose primary key is `v`, so two workers booting together race
to a unique violation that surfaces as a 500 on a user's first message; and it would
require the internet-facing process to hold DDL privileges permanently.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable

import structlog
from fastapi import Depends

from app.config import Settings, get_settings
from app.runtime import event_loop_is_psycopg_compatible

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.checkpoint.base import BaseCheckpointSaver

log = structlog.get_logger(__name__)

__all__ = [
    "CheckpointerConfigError",
    "CheckpointerError",
    "CheckpointerNotProvisionedError",
    "CheckpointerProvider",
    "MemoryCheckpointer",
    "PostgresCheckpointer",
    "apply_strict_msgpack",
    "build_checkpointer",
    "close_checkpointer",
    "ensure_checkpointer_schema",
    "get_checkpointer",
]

#: The bootstrap command named in every error message that means "not provisioned".
BOOTSTRAP_COMMAND = "uv run python -m app.services.checkpointer"

#: langgraph's own environment switch. It is read **at import time** of
#: `langgraph.checkpoint.serde._msgpack` and then snapshotted again by
#: `langgraph._internal._serde`, so it must be set before langgraph is imported anywhere —
#: patching the module attribute afterwards would miss the copy `compile()` reads.
_STRICT_MSGPACK_ENV = "LANGGRAPH_STRICT_MSGPACK"


def apply_strict_msgpack(settings: Settings | None = None) -> bool:
    """Publish `CHECKPOINTER_STRICT_MSGPACK` into langgraph's environment switch.

    langgraph ships strict msgpack **off**, and its own docstring says why that matters:
    *"any Python callable stored in checkpoint data will be imported and executed on
    load."* `checkpoints` is an ordinary table, so anyone who can write to the database
    could turn a resume into code execution. R-42(2) keeps `RAGState` to scalars and lists
    of scalars, so strict mode costs us nothing and closes that path.

    Called at import time by this module and by `app.rag.graph` — the only two modules
    that import langgraph — because the flag is captured on first import. An explicit
    `LANGGRAPH_STRICT_MSGPACK` in the environment wins, so an operator can still debug.
    """
    if _STRICT_MSGPACK_ENV in os.environ:
        return os.environ[_STRICT_MSGPACK_ENV].lower() in ("1", "true", "yes")
    enabled = (settings or get_settings()).checkpointer.strict_msgpack
    os.environ[_STRICT_MSGPACK_ENV] = "true" if enabled else "false"
    return enabled


# Applied at import: every langgraph import in this module is lazy (inside the functions
# below), so importing this module is always early enough.
apply_strict_msgpack()


# --- errors -------------------------------------------------------------------


class CheckpointerError(Exception):
    """The conversation checkpointer could not be used."""


class CheckpointerConfigError(CheckpointerError):
    """The process cannot host a checkpointer connection at all (wrong event loop, …)."""


class CheckpointerNotProvisionedError(CheckpointerError):
    """The `checkpoint*` tables are missing — the bootstrap step has not been run."""


# --- protocol -----------------------------------------------------------------


@runtime_checkable
class CheckpointerProvider(Protocol):
    """Supplies the graph's `BaseCheckpointSaver` and owns its lifetime."""

    backend: str

    async def get(self) -> BaseCheckpointSaver:
        """Return the process-wide saver, building it on first use."""
        ...

    async def aclose(self) -> None:
        """Release the saver and any connections it holds."""
        ...


# --- backends -----------------------------------------------------------------


class PostgresCheckpointer:
    """`AsyncPostgresSaver` over a psycopg pool — the FR-PER-01 production backend."""

    backend = "postgres"

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        # Derived from DATABASE_URL, never configured separately (R-42(8)).
        self._dsn = settings.database.psycopg_dsn
        self._cfg = settings.checkpointer
        self._saver: BaseCheckpointSaver | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> BaseCheckpointSaver:
        if self._saver is not None:
            return self._saver
        async with self._lock:
            if self._saver is None:
                self._reject_incompatible_event_loop()
                # Imported inside the lock, like every other seam in this package: a cold
                # dependency must not cost anything at module import.
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg.rows import dict_row
                from psycopg_pool import AsyncConnectionPool

                stack = AsyncExitStack()
                pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    min_size=self._cfg.pool_min_size,
                    max_size=self._cfg.pool_max_size,
                    timeout=self._cfg.pool_timeout_seconds,
                    kwargs={
                        # All three are load-bearing and each fails differently.
                        # autocommit: the saver's own DDL uses CREATE INDEX CONCURRENTLY,
                        #   and without it every write also sits in an unclosed txn.
                        # row_factory: the saver indexes rows by name (`row["checkpoint"]`),
                        #   which raises TypeError on the default tuple rows.
                        # prepare_threshold: server-side prepared statements break behind a
                        #   transaction-pooling pgbouncer.
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                        "connect_timeout": self._cfg.connect_timeout_seconds,
                    },
                    open=False,
                )
                try:
                    await stack.enter_async_context(pool)
                    await self._verify_provisioned(pool)
                except BaseException:
                    await stack.aclose()
                    raise
                # A pool, not `from_conn_string`: that yields a saver over a *single*
                # connection whose every operation serialises behind one lock, so every
                # concurrent chat in the process queues on it, and a server-side
                # disconnect kills it permanently with no reconnect path (NFR-REL-03).
                self._saver = AsyncPostgresSaver(pool)
                self._stack = stack
                log.info(
                    "checkpointer.ready", backend=self.backend, max_size=self._cfg.pool_max_size
                )
        return self._saver

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._saver = None

    @staticmethod
    def _reject_incompatible_event_loop() -> None:
        """Fail with an actionable message instead of psycopg's, one layer down.

        psycopg's own error is excellent but arrives with no idea what Corpus is; this one
        names the two fixes. See `app.runtime` for why Windows needs it at all.
        """
        if not event_loop_is_psycopg_compatible():
            raise CheckpointerConfigError(
                "the conversation checkpointer is psycopg-backed and cannot run on "
                "Windows' default ProactorEventLoop. Start the API with `--reload` or "
                "with `--loop app.runtime:selector_loop`."
            )

    async def _verify_provisioned(self, pool) -> None:  # noqa: ANN001 — psycopg's pool type
        """One cheap read that turns a missing bootstrap into an instruction.

        Without it, an un-provisioned database surfaces as `UndefinedTable: relation
        "checkpoints" does not exist` on someone's first chat message. One query per
        process, and no DDL — this must never quietly create what it is checking for.
        """
        from psycopg import errors as pg_errors

        async with pool.connection() as conn:
            try:
                await conn.execute("SELECT 1 FROM checkpoint_migrations LIMIT 1")
            except pg_errors.UndefinedTable as exc:
                raise CheckpointerNotProvisionedError(
                    "the LangGraph checkpointer tables are missing — run "
                    f"`{BOOTSTRAP_COMMAND}` (see deployment/README.md). They are owned by "
                    "langgraph, not Alembic, so `alembic upgrade head` does not create them."
                ) from exc


class MemoryCheckpointer:
    """In-process saver for dev and CI.

    FR-PER-01 forbids this in production and `Settings._coherent` enforces it at boot
    (R-42(9)): state lives only in this process, so every conversation dies with it and
    NFR-REL-03's stateless-API claim becomes false. Like `STORAGE_BACKEND=local` and
    `EMBEDDING_BACKEND=fake`, it is chosen by an explicit setting and never inferred.
    """

    backend = "memory"

    def __init__(self, settings: Settings | None = None) -> None:  # noqa: ARG002 — parity
        self._saver: BaseCheckpointSaver | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> BaseCheckpointSaver:
        if self._saver is not None:
            return self._saver
        async with self._lock:
            if self._saver is None:
                from langgraph.checkpoint.memory import InMemorySaver

                self._saver = InMemorySaver()
        return self._saver

    async def aclose(self) -> None:
        self._saver = None


# --- provisioning -------------------------------------------------------------


async def ensure_checkpointer_schema(settings: Settings | None = None) -> None:
    """Create or upgrade the LangGraph `checkpoint*` tables. Idempotent.

    Uses a dedicated autocommit connection rather than the shared pool: this runs the
    library's own migrations, three of which are `CREATE INDEX CONCURRENTLY` and cannot
    execute inside a transaction block.

    Also called by the test fixtures, so the bootstrap path is exercised by CI instead of
    being a README-only ritual that rots.
    """
    settings = settings or get_settings()
    PostgresCheckpointer._reject_incompatible_event_loop()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(settings.database.psycopg_dsn) as saver:
        await saver.setup()
    log.info("checkpointer.schema_ready")


def main() -> None:
    """`python -m app.services.checkpointer` — the documented bootstrap step."""
    asyncio.run(ensure_checkpointer_schema(), loop_factory=_bootstrap_loop_factory())
    print("LangGraph checkpointer tables are ready.")  # noqa: T201 — a CLI, not a service


def _bootstrap_loop_factory():  # noqa: ANN202 — asyncio.run's own parameter type
    from app.runtime import selector_loop

    return selector_loop


# --- factory / DI -------------------------------------------------------------

_provider: CheckpointerProvider | None = None


def build_checkpointer(settings: Settings | None = None) -> CheckpointerProvider:
    """Construct the backend named by ``CHECKPOINTER_BACKEND`` (no caching)."""
    settings = settings or get_settings()
    if settings.checkpointer.backend == "memory":
        return MemoryCheckpointer(settings)
    return PostgresCheckpointer(settings)


def get_checkpointer() -> CheckpointerProvider:
    """Process-wide checkpointer provider — also usable as a FastAPI dependency.

    A module global rather than `lru_cache` because `AsyncPostgresSaver.__init__` captures
    ``asyncio.get_running_loop()``: a cached instance outliving its loop is a saver bound
    to a dead one. :func:`close_checkpointer` (app lifespan / test teardown) resets it.
    """
    global _provider
    if _provider is None:
        _provider = build_checkpointer()
    return _provider


async def close_checkpointer() -> None:
    """Close and forget the cached provider."""
    global _provider
    if _provider is not None:
        await _provider.aclose()
        _provider = None


#: Route dependency for the chat surface (T-402). There is deliberately **no** extra
#: `/health/ready` arm: R-29 fixed the probe set, and this is the same Postgres the
#: `database` probe already covers — a second arm would report one outage twice. The two
#: failure modes that probe cannot see (a bad DSN, un-provisioned tables) are covered by
#: `DatabaseSettings._coherent` and `_verify_provisioned` respectively.
CheckpointerDep = Annotated[CheckpointerProvider, Depends(get_checkpointer)]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
