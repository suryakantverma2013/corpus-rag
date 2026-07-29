"""The conversation checkpointer and resume (T-301, FR-PER-01/02/03, NFR-REL-03, R-42).

DB-backed, so it skips when Postgres is unreachable — the topology and state-contract
tests live in `tests/test_graph.py` and never skip.

**These rows really commit.** The checkpointer writes through its own psycopg pool, so
nothing here joins the suite's rolled-back SQLAlchemy transaction and the `db_connection`
fixture is no help. Every test therefore takes the `thread_id` fixture, whose teardown
deletes what it wrote with `adelete_thread`.

The resume test is the point of the task. It is written so that it cannot pass by
accident: the compiled graph, the saver and the psycopg pool are all discarded between the
two halves, the state is asserted to be readable *from storage alone* before anything
resumes, and the second half asserts **which** nodes executed — so a run that silently
started over from the top fails rather than quietly looking identical.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator

import pytest

from app.config import DatabaseSettings, Settings, get_settings
from app.db.models.conversation import Conversation
from app.rag.graph import ABSTAIN_EMPTY_SCOPE, build_graph, thread_config
from app.rag.state import RAGContext
from app.services.checkpointer import (
    BOOTSTRAP_COMMAND,
    CheckpointerNotProvisionedError,
    MemoryCheckpointer,
    PostgresCheckpointer,
    build_checkpointer,
    close_checkpointer,
    ensure_checkpointer_schema,
)

OWNER_ID = uuid.uuid4()
TENANT_ID = uuid.UUID(int=0)

#: FR-PER-03 expressed in bytes. A type guard cannot catch T-307 packing chunk text into
#: an existing `list[str]`; a byte budget can. Generous on purpose — this is a smoke
#: alarm for an order-of-magnitude regression, not a tuning target.
_MAX_THREAD_CHECKPOINT_BYTES = 64 * 1024  # TBD(§8.4)


# --- test doubles -------------------------------------------------------------


class _StubSession:
    """Only what `govern` asks of a session (see `tests/test_graph.py`)."""

    def __init__(self, conversation: Conversation | None) -> None:
        self._conversation = conversation

    async def get(self, model: type, id_: uuid.UUID) -> Conversation | None:  # noqa: ARG002
        return self._conversation

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _context(conversation_id: uuid.UUID) -> RAGContext:
    conversation = Conversation(id=conversation_id, owner_id=OWNER_ID, tenant_id=TENANT_ID)
    return RAGContext(
        owner_id=OWNER_ID,
        tenant_id=TENANT_ID,
        conversation_id=conversation_id,
        sessionmaker=lambda: _StubSession(conversation),
    )


# --- pure: DSN, backend selection, the production guard -----------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/corpus",
            "postgresql://postgres:postgres@localhost:5432/corpus",
        ),
        (
            "postgresql+psycopg://u:p@db.internal:6432/corpus?sslmode=require",
            "postgresql://u:p@db.internal:6432/corpus?sslmode=require",
        ),
        # A percent-encoded password must survive byte for byte: decoding and re-encoding
        # it is the one way a scheme rewrite could silently break authentication.
        (
            "postgresql+asyncpg://user:p%40ss%3Aword@host:5432/corpus",
            "postgresql://user:p%40ss%3Aword@host:5432/corpus",
        ),
        ("postgresql://plain@host/corpus", "postgresql://plain@host/corpus"),
    ],
)
def test_psycopg_dsn_drops_the_sqlalchemy_driver(url: str, expected: str) -> None:
    """R-42(8): one setting, derived — never a second independently-set DSN."""
    assert DatabaseSettings(url=url).psycopg_dsn == expected


def test_a_non_postgres_database_url_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a PostgreSQL URL"):
        DatabaseSettings(url="sqlite+aiosqlite:///./corpus.db")


def test_build_checkpointer_honours_the_declared_backend() -> None:
    """Selection is explicit, never inferred (the `EMBEDDING_BACKEND` rule)."""
    postgres = Settings(checkpointer={"backend": "postgres"})
    memory = Settings(checkpointer={"backend": "memory"})
    assert isinstance(build_checkpointer(postgres), PostgresCheckpointer)
    assert isinstance(build_checkpointer(memory), MemoryCheckpointer)


def test_the_memory_backend_is_refused_in_production() -> None:
    """R-42(9) — the enforceable form of FR-PER-01's "never `InMemorySaver` in production".

    The failure it prevents is silent: an in-memory saver works perfectly until the
    process restarts, at which point every conversation is gone and NFR-REL-03's
    stateless-API claim is false. Boot-time refusal is the only place to catch it.
    """
    with pytest.raises(ValueError, match="CHECKPOINTER_BACKEND=memory is forbidden"):
        Settings(environment="production", checkpointer={"backend": "memory"})

    # ...and is fine everywhere else.
    assert Settings(environment="development", checkpointer={"backend": "memory"})


async def test_memory_checkpointer_satisfies_the_protocol() -> None:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    provider = MemoryCheckpointer()
    assert isinstance(await provider.get(), BaseCheckpointSaver)
    await provider.aclose()


# --- fixtures -----------------------------------------------------------------


def _dsn() -> str:
    return get_settings().database.psycopg_dsn


@pytest.fixture
async def checkpoint_schema() -> None:
    """Provision the `checkpoint*` tables, skipping the module if Postgres is down.

    Calls the **same** function `python -m app.services.checkpointer` runs, so the
    documented bootstrap is exercised by CI rather than being a README ritual that rots.
    Idempotent, so running it per test costs a handful of statements.
    """
    try:
        await ensure_checkpointer_schema()
    except Exception as exc:  # noqa: BLE001 — any failure here means "no Postgres", so skip
        pytest.skip(f"Postgres not reachable for checkpointer tests: {exc}")


@pytest.fixture
async def thread_id(checkpoint_schema: None) -> AsyncIterator[str]:
    """A throwaway thread whose checkpoint rows are deleted on teardown.

    Teardown opens its **own** short-lived saver rather than reusing the test's: the whole
    point of most tests here is that they close theirs partway through.

    No `conversations` row is created — the checkpointer holds no foreign key to app
    tables, which is what lets this suite stand entirely outside the rollback fixture.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    tid = str(uuid.uuid4())
    try:
        yield tid
    finally:
        async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
            await saver.adelete_thread(tid)


async def _blob_bytes(thread_id: str) -> int:
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(_dsn(), autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT coalesce(sum(length(blob)), 0) FROM checkpoint_blobs WHERE thread_id = %s",
            (thread_id,),
        )
        row = await cur.fetchone()
    return int(row[0])


async def _checkpoint_count(thread_id: str) -> int:
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(_dsn(), autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
        )
        row = await cur.fetchone()
    return int(row[0])


# --- provisioning -------------------------------------------------------------


async def test_the_bootstrap_is_idempotent(checkpoint_schema: None) -> None:
    """R-42(7): safe to re-run, which is what makes it a deploy step rather than a ritual."""
    from psycopg import AsyncConnection

    await ensure_checkpointer_schema()  # second time, having already run in the fixture

    async with await AsyncConnection.connect(_dsn(), autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'checkpoint%'"
        )
        tables = {row[0] for row in await cur.fetchall()}
        cur = await conn.execute("SELECT count(*) FROM checkpoint_migrations")
        applied = (await cur.fetchone())[0]

    assert tables == {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
    assert applied > 0


async def test_an_unprovisioned_database_raises_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, checkpoint_schema: None
) -> None:
    """A missing bootstrap must name the fix, not surface as `UndefinedTable` mid-chat.

    Points at the `postgres` maintenance database, which exists on every server and has
    none of our tables.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(_dsn())
    elsewhere = urlunsplit(parts._replace(path="/postgres"))

    provider = PostgresCheckpointer()
    monkeypatch.setattr(provider, "_dsn", elsewhere)

    with pytest.raises(CheckpointerNotProvisionedError) as excinfo:
        await provider.get()
    await provider.aclose()

    assert BOOTSTRAP_COMMAND in str(excinfo.value)


# --- FR-PER-02: thread_id is the conversation id ------------------------------


async def test_thread_id_is_the_conversation_id(thread_id: str) -> None:
    """FR-PER-02, proven at the storage layer rather than at the call site.

    Reads `checkpoints` with raw psycopg keyed on the *conversation* UUID: if the graph
    ever derived a thread id instead of using the id itself, this finds no rows.
    """
    conversation_id = uuid.UUID(thread_id)
    provider = build_checkpointer()
    graph = build_graph(await provider.get())

    await graph.ainvoke(
        {"query": "hello", "turn_index": 0},
        thread_config(conversation_id),
        context=_context(conversation_id),
        durability="sync",
    )
    await provider.aclose()

    assert await _checkpoint_count(str(conversation_id)) > 0


# --- FR-PER-01: resume ---------------------------------------------------------


async def test_a_turn_resumes_after_the_process_is_discarded(thread_id: str) -> None:
    """The task's headline requirement (FR-PER-01, NFR-REL-03).

    Structured so it cannot pass by accident:

    1. run the **production** graph to a partial state (no test-only topology);
    2. discard the compiled graph, the saver and the psycopg pool;
    3. build a wholly fresh provider, pool and compilation;
    4. assert the state is readable *before* anything resumes — that is the proof it came
       from storage rather than from a live object;
    5. resume with `None` and assert **which** nodes ran: a run that started over would
       execute the prologue again and look otherwise identical;
    6. assert one `source == "input"` checkpoint for the whole thread — a second run would
       write a second one.
    """
    conversation_id = uuid.UUID(thread_id)
    config = thread_config(conversation_id)
    context = _context(conversation_id)

    provider1 = build_checkpointer()
    graph1 = build_graph(await provider1.get())
    async for _ in graph1.astream(
        {"query": "what do my documents say?", "turn_index": 0},
        config,
        context=context,
        durability="sync",
        interrupt_after=["retrieve"],
    ):
        pass
    before = await graph1.aget_state(config)
    assert before.next == ("rerank",)
    assert "retrieved_chunk_ids" in before.values

    # 2. nothing from the first half survives.
    await close_checkpointer()
    await provider1.aclose()
    del graph1, provider1

    # 3. a fresh process-equivalent.
    provider2 = build_checkpointer()
    graph2 = build_graph(await provider2.get())
    try:
        # 4. read back from storage alone.
        after = await graph2.aget_state(config)
        assert after.values == before.values
        assert after.next == before.next

        # 5. resume — and observe exactly which nodes execute.
        executed: list[str] = []
        async for chunk in graph2.astream(
            None, config, context=context, durability="sync", stream_mode="updates"
        ):
            executed.extend(chunk)
        assert executed == ["rerank", "generate", "gate", "finalize"]

        final = await graph2.aget_state(config)
        assert final.values["outcome"] == "abstained"
        assert final.values["answer"] == ABSTAIN_EMPTY_SCOPE

        # 6. one input checkpoint => a continuation, not a second run.
        sources = [
            snapshot.metadata["source"] async for snapshot in graph2.aget_state_history(config)
        ]
        assert sources.count("input") == 1
    finally:
        await provider2.aclose()


_CHILD_SCRIPT = """
import asyncio, selectors, sys, uuid

async def main(thread_id):
    from app.db.models.conversation import Conversation
    from app.rag.graph import build_graph, thread_config
    from app.rag.state import RAGContext
    from app.services.checkpointer import build_checkpointer

    conversation_id = uuid.UUID(thread_id)
    owner_id = uuid.UUID({owner!r})
    conversation = Conversation(id=conversation_id, owner_id=owner_id, tenant_id=uuid.UUID(int=0))

    class S:
        async def get(self, model, id_):
            return conversation
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    provider = build_checkpointer()
    graph = build_graph(await provider.get())
    ctx = RAGContext(
        owner_id=owner_id,
        tenant_id=uuid.UUID(int=0),
        conversation_id=conversation_id,
        sessionmaker=lambda: S(),
    )
    async for _ in graph.astream(
        {{"query": "written by another process", "turn_index": 0}},
        thread_config(conversation_id),
        context=ctx,
        durability="sync",
        interrupt_after=["retrieve"],
    ):
        pass
    await provider.aclose()

asyncio.run(
    main(sys.argv[1]),
    loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
)
"""


async def test_a_checkpoint_survives_a_real_process_exit(thread_id: str) -> None:
    """The literal reading of "resume after restart" — a separate OS process.

    The in-process test above discards its objects, but both halves still share one
    interpreter, one import graph and one settings cache. Here the first half runs in a
    child that exits before this process reads anything, so nothing but the database can
    be carrying the state. This is NFR-REL-03's claim, tested rather than asserted.
    """
    script = textwrap.dedent(_CHILD_SCRIPT.format(owner=str(OWNER_ID)))
    # Blocking on purpose, and `asyncio.create_subprocess_exec` is not an option:
    # Windows' selector loop — which this suite pins so psycopg works at all — has no
    # subprocess transport, so the async API raises `NotImplementedError` there. Blocking
    # the loop while waiting for a child is exactly what this test wants anyway.
    completed = subprocess.run(  # noqa: ASYNC221, S603 — our own interpreter, our own script
        [sys.executable, "-c", script, thread_id],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    conversation_id = uuid.UUID(thread_id)
    config = thread_config(conversation_id)
    provider = build_checkpointer()
    graph = build_graph(await provider.get())
    try:
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("rerank",)
        assert snapshot.values["query"] == "written by another process"

        executed: list[str] = []
        async for chunk in graph.astream(
            None,
            config,
            context=_context(conversation_id),
            durability="sync",
            stream_mode="updates",
        ):
            executed.extend(chunk)
        assert executed == ["rerank", "generate", "gate", "finalize"]
    finally:
        await provider.aclose()


async def test_interrupt_state_survives_a_restart(thread_id: str) -> None:
    """FR-PER-01's human-in-the-loop clause, which nothing else in T-301 exercises.

    `aupdate_state(as_node="gate")` forces the verdict T-301's gate stub never produces,
    so the `review` node and its `interrupt` are reachable today rather than at T-308.
    """
    from langgraph.types import Command

    conversation_id = uuid.UUID(thread_id)
    config = thread_config(conversation_id)
    context = _context(conversation_id)

    provider1 = build_checkpointer()
    graph1 = build_graph(await provider1.get())
    async for _ in graph1.astream(
        {"query": "needs a human", "turn_index": 0},
        config,
        context=context,
        durability="sync",
        interrupt_after=["generate"],
    ):
        pass
    await graph1.aupdate_state(config, {"gate_verdict": "review"}, as_node="gate")
    async for _ in graph1.astream(None, config, context=context, durability="sync"):
        pass
    await provider1.aclose()
    del graph1, provider1

    provider2 = build_checkpointer()
    graph2 = build_graph(await provider2.get())
    try:
        paused = await graph2.aget_state(config)
        assert paused.next == ("review",)
        assert paused.interrupts, "the interrupt payload did not survive the restart"
        # FR-PER-03 applies to the interrupt payload too: ids, not chunk text.
        assert set(paused.interrupts[0].value) == {"reason", "turn_index", "chunk_ids"}

        async for _ in graph2.astream(
            Command(resume={"decision": "approve"}),
            config,
            context=context,
            durability="sync",
        ):
            pass
        assert (await graph2.aget_state(config)).values["outcome"] == "answered"
    finally:
        await provider2.aclose()


async def test_a_second_turn_sees_the_first_turns_state(thread_id: str) -> None:
    """FR-PER-02: all turns of a chat share one thread, so turn 2 reads turn 1's state."""
    conversation_id = uuid.UUID(thread_id)
    config = thread_config(conversation_id)
    context = _context(conversation_id)

    provider = build_checkpointer()
    graph = build_graph(await provider.get())
    try:
        await graph.ainvoke(
            {"query": "first", "turn_index": 0}, config, context=context, durability="sync"
        )
        await graph.ainvoke(
            {"query": "second", "turn_index": 1}, config, context=context, durability="sync"
        )
        values = (await graph.aget_state(config)).values
        assert values["turn_index"] == 1
        assert values["query"] == "second"
    finally:
        await provider.aclose()


# --- FR-PER-03, measured ------------------------------------------------------


async def test_a_thread_checkpoints_less_than_the_lightweight_budget(thread_id: str) -> None:
    """FR-PER-03 in bytes, which is the only form that survives T-305..T-307.

    The static guards in `test_graph.py` police the *shape* of the state; they cannot see
    a later task appending chunk text to `reranked_chunk_ids`, which is a `list[str]`
    either way. Several turns on one thread is where an unbounded channel shows up, since
    langgraph re-serialises a channel in full every time it changes.
    """
    conversation_id = uuid.UUID(thread_id)
    config = thread_config(conversation_id)
    context = _context(conversation_id)

    provider = build_checkpointer()
    graph = build_graph(await provider.get())
    try:
        for turn in range(3):
            await graph.ainvoke(
                {"query": f"turn {turn}", "turn_index": turn},
                config,
                context=context,
                durability="sync",
            )
    finally:
        await provider.aclose()

    total = await _blob_bytes(thread_id)
    assert total < _MAX_THREAD_CHECKPOINT_BYTES, (
        f"three turns checkpointed {total:,} bytes, over the {_MAX_THREAD_CHECKPOINT_BYTES:,} "
        "FR-PER-03 budget — something heavy entered RAGState"
    )


async def test_adelete_thread_is_scoped_to_one_thread(thread_id: str) -> None:
    """Proves both the T-401 deletion primitive and that this suite's cleanup works.

    R-42(11) makes `adelete_thread` T-401's obligation on conversation delete — without
    it, FR-SBR-07 leaves the user's questions in the checkpointer indefinitely.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    other = str(uuid.uuid4())
    provider = build_checkpointer()
    graph = build_graph(await provider.get())
    try:
        for tid in (thread_id, other):
            cid = uuid.UUID(tid)
            await graph.ainvoke(
                {"query": "hi", "turn_index": 0},
                thread_config(cid),
                context=_context(cid),
                durability="sync",
            )
        assert await _checkpoint_count(thread_id) > 0
        assert await _checkpoint_count(other) > 0

        async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
            await saver.adelete_thread(other)

        assert await _checkpoint_count(other) == 0
        assert await _checkpoint_count(thread_id) > 0, "deleting one thread took out another"
    finally:
        await provider.aclose()
        async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
            await saver.adelete_thread(other)


# --- platform (R-42(12)) ------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="ProactorEventLoop is Windows-only")
def test_a_proactor_loop_is_refused_with_an_actionable_message() -> None:
    """R-42(12): the psycopg/ProactorEventLoop trap, caught before psycopg sees it.

    A bare `uvicorn app.main:app` on Windows selects the Proactor loop, and every chat
    turn would then die inside the checkpointer. The message has to name the fix, because
    the raw psycopg error gives no hint that Corpus has an option.
    """
    from app.services.checkpointer import CheckpointerConfigError

    async def _probe() -> None:
        with pytest.raises(CheckpointerConfigError, match="app.runtime:selector_loop"):
            await PostgresCheckpointer().get()

    asyncio.run(_probe(), loop_factory=asyncio.ProactorEventLoop)


def test_the_selector_loop_factory_is_psycopg_compatible() -> None:
    from app.runtime import event_loop_is_psycopg_compatible, selector_loop

    loop = selector_loop()
    try:
        assert event_loop_is_psycopg_compatible(loop)
    finally:
        loop.close()


def test_strict_msgpack_is_enabled() -> None:
    """R-42: langgraph ships it off, and off means a checkpoint payload can name a
    callable that is imported and executed on load."""
    import langgraph._internal._serde as lg_serde

    assert lg_serde.STRICT_MSGPACK_ENABLED is True
