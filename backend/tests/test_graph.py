"""The orchestrator graph's topology and state contract (T-301, §4.12, FR-ORC-01/07, R-42).

**No database and no network.** `build_state_graph()` is pure, and the behaviour tests run
the compiled graph on an `InMemorySaver` with a stub sessionmaker, so this whole file runs
on a machine with nothing installed. The DB-backed half — the checkpointer, resume and
restart — is `tests/test_checkpointer.py`, split for the same reason `test_fusion.py` is
separate from `test_retrieval.py`: a Postgres outage must not silently skip the tests that
guard a *contract*.

The FR-PER-03 guards at the bottom are the reason this file exists. `RAGState` is read and
written by nine later tasks, and the failure they prevent is quiet: a field holding chunk
text costs nothing on the day it lands and shows up months later as bloated checkpoints
and slow resumes. Making it a test failure is the only way that stays true.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import uuid
from collections import deque
from pathlib import Path
from typing import Literal, get_args, get_origin, get_type_hints

import pytest
import structlog
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START

from app.config import get_settings
from app.db.models.audit_log import AuditLog
from app.db.models.conversation import Conversation
from app.rag import graph as graph_module
from app.rag import telemetry
from app.rag.errors import FAILURE_COPY, FailureClass, classify, copy_for
from app.rag.graph import (
    ABSTAIN_EMPTY_SCOPE,
    ACCESS_DENIED,
    NODE_NAMES,
    SYSTEM_FAILURE,
    build_graph,
    build_state_graph,
    decide_after_gate,
    thread_config,
)
from app.rag.state import RAGContext, RAGState
from app.services.processing_lock import MemoryProcessingLockStore

OWNER_ID = uuid.uuid4()
TENANT_ID = uuid.UUID(int=0)


# --- test doubles -------------------------------------------------------------


class _StubSession:
    """The calls `govern` makes on a session, and nothing else.

    A test-local double injected through `RAGContext`, following the `_StubScanner` /
    `_FakePool` convention. Keeping it this thin is deliberate: the moment a node needs
    more of a session than this, it needs a real one, and the test belongs in the
    DB-backed file instead.

    `add`/`flush`/`commit` exist for the NFR-SEC-08 denial write (T-302). ``added``
    accumulates across every session the run opens, because the sessionmaker hands out the
    *same* stub — which is what lets a test assert the audit row without a database.
    """

    def __init__(self, conversation: Conversation | None) -> None:
        self._conversation = conversation
        self.added: list[object] = []
        self.commits = 0

    async def get(self, model: type, id_: uuid.UUID) -> Conversation | None:  # noqa: ARG002
        return self._conversation

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _context(
    *,
    owner_id: uuid.UUID = OWNER_ID,
    conversation_owner: uuid.UUID | None = None,
    session: _StubSession | None = None,
    lock: MemoryProcessingLockStore | None = None,
):
    """A `RAGContext` whose stub conversation is owned by `conversation_owner`.

    The lock store is constructed per context rather than as a module global, so no autouse
    reset fixture is needed (contrast `_reset_stream_registry` in `conftest`).
    """
    conversation_id = uuid.uuid4()
    owner = conversation_owner if conversation_owner is not None else owner_id
    conversation = Conversation(id=conversation_id, owner_id=owner, tenant_id=TENANT_ID)
    stub = session or _StubSession(conversation)
    return RAGContext(
        owner_id=owner_id,
        tenant_id=TENANT_ID,
        conversation_id=conversation_id,
        sessionmaker=lambda: stub,
        processing_lock=lock or MemoryProcessingLockStore(),
    )


async def _run(context: RAGContext, saver: InMemorySaver | None = None, **state: object):
    """Run one turn to completion; return `(executed_node_names, final_state_values)`."""
    compiled = build_graph(saver or InMemorySaver())
    config = thread_config(context.conversation_id)
    payload: dict = {"query": "what do my documents say?", "turn_index": 0}
    payload.update(state)
    executed: list[str] = []
    async for chunk in compiled.astream(
        payload, config, context=context, stream_mode="updates", durability="sync"
    ):
        executed.extend(chunk)
    snapshot = await compiled.aget_state(config)
    return executed, snapshot.values


# --- topology (FR-ORC-01, FR-ORC-07) ------------------------------------------


def test_node_set_is_exactly_the_fr_orc_01_and_07_steps() -> None:
    """The §4.12 spine plus the FR-ORC-07 additions — no more, no fewer.

    Asserted against a *fresh* builder: `compile()` mutates the builder it is called on
    (it appends `__default_error_handler__`), which is why `build_state_graph` returns a
    new one every call.
    """
    assert set(build_state_graph().nodes) == set(NODE_NAMES)


def test_finalize_is_the_only_edge_to_end() -> None:
    """§4.12 precedence note (2), as reachability.

    The pseudocode's finalization sits in a `finally`, so no terminal path may skip it.
    In a graph that means exactly one thing: `END` has a single predecessor. Denial,
    injection block, abstain, exhausted retry, human review and node exceptions all route
    through `finalize`, which is the sole releaser of the R-24 lock (FR-ORC-04's "the UI
    is always unlocked on completion, success or failure").
    """
    builder = build_state_graph()
    direct = {src for src, dst in builder.edges if dst == END}
    assert direct == {"finalize"}

    conditional_targets = {
        target
        for branches in builder.branches.values()
        for branch in branches.values()
        for target in (branch.ends or {}).values()
    }
    assert END not in conditional_targets, "a conditional edge bypasses finalize"


def _successors(builder) -> dict[str, set[str]]:  # noqa: ANN001 — StateGraph is generic
    graph: dict[str, set[str]] = {}
    for src, dst in builder.edges:
        graph.setdefault(src, set()).add(dst)
    for src, branches in builder.branches.items():
        for branch in branches.values():
            graph.setdefault(src, set()).update((branch.ends or {}).values())
    return graph


def test_no_path_reaches_retrieval_without_screening() -> None:
    """FR-ORC-07 / NFR-SEC-05: the injection screen sits *before* retrieval.

    A BFS over every path rather than a look at the happy one — the risk is a future task
    adding a shortcut edge (an @-mention fast path, a cache hit) that quietly skips the
    screen for some queries only.
    """
    successors = _successors(build_state_graph())
    queue = deque([(START, False)])
    seen: set[tuple[str, bool]] = set()
    while queue:
        node, screened = queue.popleft()
        if (node, screened) in seen:
            continue
        seen.add((node, screened))
        for nxt in successors.get(node, set()):
            assert not (nxt == "retrieve" and not screened), (
                "a path reaches `retrieve` without passing `screen` (FR-ORC-07)"
            )
            queue.append((nxt, screened or nxt == "screen"))


def test_adapt_is_the_only_way_back_into_retrieve() -> None:
    """Half of the FR-ORC-07 termination proof: every cycle passes through `adapt`."""
    successors = _successors(build_state_graph())
    predecessors = {src for src, targets in successors.items() if "retrieve" in targets}
    assert predecessors == {"route", "adapt"}


def test_adapt_is_the_only_node_that_writes_retry_count() -> None:
    """The other half. A source scan, in the established R-31 guard-test style.

    `decide_after_gate` compares `retry_count` against the bound, so a second writer would
    not fail any behavioural test — it would just make the bound wrong, and the graph
    would loop until langgraph's recursion limit turned an abstention into a 500.
    """
    writers = [
        name
        for name in NODE_NAMES
        if '"retry_count":' in inspect.getsource(getattr(graph_module, name))
    ]
    assert writers == ["adapt"], f"{writers} write retry_count; only `adapt` may (FR-ORC-07)"
    # Belt and braces: nothing outside `adapt` assigns it either — an error handler or a
    # helper could write the channel without being a node.
    source = Path(graph_module.__file__).read_text(encoding="utf-8")
    assert source.count('"retry_count":') == 1


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"gate_verdict": "pass"}, "finalize"),
        ({"gate_verdict": "review"}, "review"),
        ({"gate_verdict": "retry", "retry_count": 0}, "adapt"),
        ({"gate_verdict": "retry", "retry_count": 1}, "abstain"),
        ({"gate_verdict": "retry", "retry_count": 99}, "abstain"),
        ({"gate_verdict": "abstain"}, "abstain"),
        ({}, "abstain"),
    ],
)
def test_decide_after_gate_routes_every_verdict(state: RAGState, expected: str) -> None:
    """FR-ORC-07's conditional edge, including the exhausted-budget case.

    `retry_count == max_retries` must route to `abstain`, not `adapt`: a `>=` comparison
    written the other way round gives one extra cycle than configured, which is the kind
    of off-by-one that only shows up as a latency complaint.
    """
    assert decide_after_gate(state, max_retries=1) == expected


def test_recursion_limit_admits_the_worst_case_path() -> None:
    """GRAPH_RECURSION_LIMIT must fit the longest run GRAPH_MAX_RETRIES allows.

    Not a theoretical bound: exceeding it raises `GraphRecursionError`, so a limit that is
    too low converts the FR-RET-05 abstain path — the *graceful* outcome — into an
    unhandled 500. Computed from the topology so raising `GRAPH_MAX_RETRIES` in a `.env`
    fails here rather than in production.
    """
    settings = get_settings()
    prologue = 4  # govern, telemetry_start, lock, screen
    cycle = 4  # retrieve, rerank, generate, gate  (+ route on the first pass)
    epilogue = 2  # abstain, finalize
    worst_case = (
        prologue + 1 + cycle * (settings.graph.max_retries + 1) + settings.graph.max_retries
    ) + epilogue
    assert worst_case < settings.graph.recursion_limit, (
        f"GRAPH_RECURSION_LIMIT={settings.graph.recursion_limit} cannot fit the "
        f"{worst_case}-superstep worst case at GRAPH_MAX_RETRIES={settings.graph.max_retries}"
    )


# --- behaviour (R-23, FR-ORC-02, FR-ORC-05) -----------------------------------


async def test_an_empty_scope_abstains_and_never_fabricates() -> None:
    """R-23 / FR-SYS-02 / §4.12 precedence note (3).

    With no retriever wired the scope is empty, which is a legitimate state of the world
    (a user who has uploaded nothing) — not a stub artefact. Always-on grounding means the
    only correct response is to say so. The assertion that matters is the last one: the
    answer is the abstain copy, so no pre-training answer can slip through.
    """
    executed, values = await _run(_context())
    assert executed == [
        "govern",
        "telemetry_start",
        "lock",
        "screen",
        "route",
        "retrieve",
        "rerank",
        "generate",
        "gate",
        "finalize",
    ]
    assert values["outcome"] == "abstained"
    assert values["retrieved_chunk_ids"] == []
    assert values["citation_ids"] == []
    assert values["answer"] == ABSTAIN_EMPTY_SCOPE


async def test_a_denied_turn_still_reaches_finalize() -> None:
    """FR-ORC-02 stops processing; §4.12(2) still requires finalization.

    The conversation belongs to someone else, so `govern` denies. Note what is *not* in
    the executed list: nothing after `govern` except `finalize` — no telemetry span is
    opened, no lock taken, no retrieval performed.
    """
    executed, values = await _run(_context(conversation_owner=uuid.uuid4()))
    assert executed == ["govern", "finalize"]
    assert values["outcome"] == "blocked"
    assert values["answer"] == ACCESS_DENIED
    assert values["error_code"] == "ACCESS_DENIED"


async def test_a_missing_conversation_is_denied_not_crashed() -> None:
    """A resumed run whose conversation was deleted must deny, not raise."""
    context = RAGContext(
        owner_id=OWNER_ID,
        tenant_id=TENANT_ID,
        conversation_id=uuid.uuid4(),
        sessionmaker=lambda: _StubSession(None),
    )
    _, values = await _run(context)
    assert values["outcome"] == "blocked"


async def test_an_admin_may_run_another_users_conversation() -> None:
    """The `is_admin` half of the ownership check, which lives only in `RAGContext`."""
    conversation_id = uuid.uuid4()
    conversation = Conversation(id=conversation_id, owner_id=uuid.uuid4(), tenant_id=TENANT_ID)
    context = RAGContext(
        owner_id=OWNER_ID,
        tenant_id=TENANT_ID,
        conversation_id=conversation_id,
        sessionmaker=lambda: _StubSession(conversation),
        is_admin=True,
    )
    _, values = await _run(context)
    assert values["outcome"] == "abstained"


async def test_a_raising_node_lands_in_finalize_with_a_failure_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The §4.12 CATCH block (FR-ORC-05 / FR-ERR-04).

    A node exception must not kill the run: it becomes a failure class, and the run still
    finalizes so the R-24 lock is released. Asserting `error_code` is a *class* rather than
    a message is the FR-PER-03 point — a traceback must never reach the checkpoint.

    Until T-302 this asserted `"RuntimeError"`, the exception's Python type name. That was
    neither a class of failure FR-ERR-04 could attach copy to nor anything a user could act
    on, and it put internal type names into a durable store.
    """

    async def _boom(state: RAGState, runtime: object) -> RAGState:
        raise RuntimeError("retrieval exploded")

    monkeypatch.setattr(graph_module, "retrieve", _boom)
    executed, values = await _run(_context())

    assert "finalize" in executed
    assert values["outcome"] == "error"
    assert values["error_code"] == FailureClass.SYSTEM_FAILURE.value
    assert values["answer"] == SYSTEM_FAILURE
    assert "RuntimeError" not in str(values), "a Python type name reached the checkpoint"
    assert "exploded" not in str(values), "the exception message reached the checkpoint"


# --- the R-24 processing lock (FR-STA-02 / FR-ORC-04, R-43) -------------------


@pytest.mark.parametrize(
    ("label", "patch", "state"),
    [
        ("abstain-empty-scope", None, {}),
        ("injection-blocked", "screen", {}),
        ("node-error", "retrieve", {}),
        ("human-review", "gate", {}),
    ],
)
async def test_the_lock_is_released_on_every_terminal_path(
    monkeypatch: pytest.MonkeyPatch,
    label: str,  # noqa: ARG001 — names the case in pytest output
    patch: str | None,
    state: dict,
) -> None:
    """FR-ORC-04's "always unlocked on completion, success or failure", executably.

    R-42(5) made `finalize` the only edge to `END` so that every terminal path passes
    through the node that releases; this is the assertion that turns that topology claim
    into a behavioural one. It is the highest-value test in T-302 — a release that only
    happened on the happy path would leave a user unable to touch their own knowledge base
    until the TTL expired.
    """

    async def _blocked(state: RAGState, runtime: object) -> RAGState:
        return {"injection_verdict": "blocked"}

    async def _boom(state: RAGState, runtime: object) -> RAGState:
        raise RuntimeError("boom")

    async def _review(state: RAGState, runtime: object) -> RAGState:
        return {"gate_verdict": "review", "gate_reason": "LOW_GROUNDEDNESS"}

    if patch is not None:
        monkeypatch.setattr(
            graph_module, patch, {"screen": _blocked, "retrieve": _boom, "gate": _review}[patch]
        )

    context = _context()
    lock = context.processing_lock
    assert isinstance(lock, MemoryProcessingLockStore)

    if patch == "gate":
        # A review interrupt parks the run; resume it so the turn actually terminates.
        compiled = build_graph(InMemorySaver())
        config = thread_config(context.conversation_id)
        await compiled.ainvoke(
            {"query": "q", "turn_index": 0}, config, context=context, durability="sync"
        )
        from langgraph.types import Command as _Command

        await compiled.ainvoke(
            _Command(resume={"decision": "reject"}), config, context=context, durability="sync"
        )
    else:
        await _run(context, **state)

    assert [call[0] for call in lock.calls][:1] == ["acquire"]
    assert ("release", context.owner_id, lock.calls[0][2]) in lock.calls
    assert lock.held == {}, "the R-24 gate outlived the turn"


async def test_a_denied_turn_takes_no_lock_and_opens_no_span() -> None:
    """FR-ORC-02 denies before `lock` and before `telemetry_start` both run.

    `finalize` therefore has neither a token to release nor a span to close, and must
    tolerate both absences — the second is not hypothetical, since a latency computed from
    a missing `started_at` would raise inside the one node R-42(5) makes unskippable.
    """
    context = _context(conversation_owner=uuid.uuid4())
    lock = context.processing_lock
    assert isinstance(lock, MemoryProcessingLockStore)

    with structlog.testing.capture_logs() as logs:
        executed, values = await _run(context)

    assert executed == ["govern", "finalize"]
    assert lock.calls == []
    assert values["outcome"] == "blocked"
    assert "started_at" not in values

    events = {entry["event"] for entry in logs}
    assert telemetry.TURN_DENIED in events
    assert telemetry.TURN_START not in events
    assert telemetry.TURN_END not in events, "an end with no start breaks span pairing"


async def test_a_foreign_owner_is_audited_but_a_missing_conversation_is_not() -> None:
    """NFR-SEC-08 records the security event and not the routine one (R-43(7)).

    A conversation that no longer exists is a resumed run whose chat was deleted
    (R-42(11)/T-401) — ordinary lifecycle, and filling a security artefact with it would
    make the trail less useful, not more.
    """
    conversation_id = uuid.uuid4()
    foreign = Conversation(id=conversation_id, owner_id=uuid.uuid4(), tenant_id=TENANT_ID)
    session = _StubSession(foreign)
    await _run(_context(conversation_owner=uuid.uuid4(), session=session))

    rows = [row for row in session.added if isinstance(row, AuditLog)]
    assert len(rows) == 1
    assert rows[0].details["action"] == "chat_access_denied"
    assert rows[0].target_type == "conversation"
    assert session.commits == 1

    missing = _StubSession(None)
    await _run(_context(session=missing))
    assert [row for row in missing.added if isinstance(row, AuditLog)] == []


async def test_a_second_turn_overwrites_and_the_first_release_is_a_no_op() -> None:
    """Two overlapping turns (a second tab, or Regenerate over a live one).

    Last-writer-wins acquire plus token-matched release means the gate holds until the
    *later* turn ends, whichever order they finish in. Releasing by `owner_id` alone would
    free the live turn the moment the stale one finished.
    """
    lock = MemoryProcessingLockStore()
    await lock.acquire(owner_id=OWNER_ID, conversation_id=None, token="first")
    await lock.acquire(owner_id=OWNER_ID, conversation_id=None, token="second")

    assert await lock.release(owner_id=OWNER_ID, token="first") is False
    assert lock.held[OWNER_ID] == "second", "the live turn's gate was freed by a stale one"
    assert await lock.release(owner_id=OWNER_ID, token="second") is True
    assert lock.held == {}


async def test_a_lock_store_outage_degrades_the_gate_and_never_the_turn() -> None:
    """R-43(3): the lock is advisory, so `lock` fails open rather than failing the turn.

    R-24 places consistency on FR-ING-04/05 + FR-RET-04 and citation validity on serve-time
    FR-CIT-06, so nothing downstream depends on the gate being published. Raising here would
    route through `handle_node_error` and turn a live upload button into a broken chat.
    """

    class _Broken(MemoryProcessingLockStore):
        async def acquire(self, **kwargs: object) -> None:
            raise RuntimeError("store down")

    context = _context(lock=_Broken())
    executed, values = await _run(context)

    assert values["outcome"] == "abstained", "a gate outage failed the turn"
    assert values["lock_token"] is None
    assert executed[-1] == "finalize"


# --- telemetry (FR-ORC-03 / NFR-OBS-01, R-43) ---------------------------------


async def test_telemetry_pairs_a_start_with_an_end_and_carries_no_payload_text() -> None:
    """The R-43(5) event contract, which binds T-604.

    The "no payload text" half is FR-PER-03's principle applied to the log stream, where it
    matters more than in the checkpointer because logs leave the machine.
    """
    with structlog.testing.capture_logs() as logs:
        await _run(_context(), query="a very distinctive question string")

    emitted = [entry for entry in logs if entry["event"] in telemetry.EVENT_NAMES]
    assert [entry["event"] for entry in emitted] == [telemetry.TURN_START, telemetry.TURN_END]

    end = emitted[-1]
    assert end["outcome"] == "abstained", "an abstention is a response, not a failure"
    assert isinstance(end["latency_ms"], int)

    blob = str(logs)
    assert "distinctive question" not in blob
    assert ABSTAIN_EMPTY_SCOPE not in blob


async def test_an_errored_turn_closes_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.failure` is reserved for `outcome == "error"` — abstentions are not incidents."""

    async def _boom(state: RAGState, runtime: object) -> RAGState:
        raise RuntimeError("boom")

    monkeypatch.setattr(graph_module, "retrieve", _boom)
    with structlog.testing.capture_logs() as logs:
        await _run(_context())

    emitted = [entry["event"] for entry in logs if entry["event"] in telemetry.EVENT_NAMES]
    assert emitted == [telemetry.TURN_START, telemetry.TURN_FAILURE]


# --- FR-ORC-05 failure classes ------------------------------------------------


def test_every_failure_class_has_copy() -> None:
    """Totality — a class with no message is a class that renders as nothing."""
    assert set(FAILURE_COPY) == set(FailureClass)
    assert all(text.strip() for text in FAILURE_COPY.values())


def test_an_unmapped_exception_is_the_fr_err_04_fallback() -> None:
    assert classify(Exception("who knows")) is FailureClass.SYSTEM_FAILURE
    assert copy_for(None) == SYSTEM_FAILURE
    assert copy_for("SOMETHING_A_NEWER_DEPLOY_WROTE") == SYSTEM_FAILURE


def test_subsystem_codes_map_to_user_facing_classes() -> None:
    """`classify` reuses the `code: ClassVar[str]` those exception trees already carry.

    Embedding failures collapse into one user-facing class on purpose — the operator's fix
    differs, but "we couldn't search your documents" is one instruction — except throttling,
    which earns its own because "wait a moment" is a different instruction.
    """
    from app.services.embeddings import EmbeddingRateLimitedError, EmbeddingUnavailableError

    assert classify(EmbeddingUnavailableError()) is FailureClass.RETRIEVAL_UNAVAILABLE
    assert classify(EmbeddingRateLimitedError()) is FailureClass.RATE_LIMITED
    assert classify(TimeoutError()) is FailureClass.TIMEOUT
    assert copy_for(FailureClass.TIMEOUT.value) == FAILURE_COPY[FailureClass.TIMEOUT]


def test_access_denied_is_not_a_failure_class() -> None:
    """FR-ORC-02 is a governance *outcome*; folding it in would overload `outcome == error`."""
    assert "ACCESS_DENIED" not in {member.value for member in FailureClass}
    assert copy_for("ACCESS_DENIED") == ACCESS_DENIED


async def test_a_blocked_prompt_never_reaches_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-SEC-05: a blocked injection short-circuits to abstain, before any retrieval."""

    async def _blocked(state: RAGState, runtime: object) -> RAGState:
        return {"injection_verdict": "blocked", "injection_rule": "TEST_RULE"}

    monkeypatch.setattr(graph_module, "screen", _blocked)
    executed, values = await _run(_context())

    assert "retrieve" not in executed
    assert "generate" not in executed
    assert executed[-2:] == ["abstain", "finalize"]
    assert values["outcome"] == "blocked"


async def test_finalize_clears_the_answer_once_it_is_persisted() -> None:
    """R-42(2): the state *at rest* — what the next turn loads — carries no answer text.

    Clearing is conditional on `answer_message_id` on purpose. Until T-402 persists the
    `messages` row, the checkpoint holds the run's only copy of the answer, and dropping
    it because the persist step has not happened yet would turn a recoverable failure into
    lost work. Both directions are asserted here so a later task cannot flip one silently.
    """
    _, kept = await _run(_context())
    assert kept["answer"] == ABSTAIN_EMPTY_SCOPE  # not yet persisted anywhere

    _, cleared = await _run(_context(), answer_message_id=str(uuid.uuid4()))
    assert cleared["answer"] is None
    assert cleared["answer_message_id"] is not None


# --- the FR-PER-03 lightweight-state contract ---------------------------------

#: Everything a checkpointed field may be.
_SCALARS = {str, int, float, bool, type(None)}

#: Substrings that would smuggle a payload past the type check — a `list[str]` of chunk
#: *texts* type-checks perfectly. Matched as substrings rather than `_`-separated tokens
#: so plurals and compounds (`retrieved_chunk_texts`) cannot slip through on a token that
#: happens not to be in the set.
_BANNED_NAME_PARTS = frozenset(
    {
        "text",
        "content",
        "chunks",
        "documents",
        "embedding",
        "vector",
        "context",
        "message",
        "history",
        "transcript",
        "image",
        "payload",
        "blob",
        "quote",
        "passage",
    }
)

#: The one `list[float]`. Anything else of that shape is an embedding in disguise.
_FLOAT_LIST_ALLOWLIST = frozenset({"rerank_scores"})

#: The T-301 contract binding T-302..T-310 (R-42(2)). Adding an optional key is safe — an
#: existing checkpoint simply has no value for the new channel. **Renaming, retyping or
#: removing one is breaking**: it orphans every conversation that is mid-turn when the
#: deploy lands. Editing this set is therefore a contract change and needs a note on the
#: changing task's board line, in the same commit.
_FROZEN_FIELDS = frozenset(
    {
        "query",
        "user_message_id",
        "turn_index",
        "mentioned_document_ids",
        "started_at",
        "lock_token",
        "injection_verdict",
        "injection_rule",
        "query_class",
        "strategy",
        "sub_queries",
        "retrieved_chunk_ids",
        "reranked_chunk_ids",
        "rerank_scores",
        "answer",
        "model_name",
        "prompt_tokens",
        "completion_tokens",
        "groundedness",
        "gate_verdict",
        "gate_reason",
        "citation_ids",
        "retry_count",
        "outcome",
        "error_code",
        "answer_message_id",
    }
)


def test_rag_state_field_names_are_frozen() -> None:
    """R-42(2). See `_FROZEN_FIELDS` — this failing is a contract change, not a bug."""
    assert set(get_type_hints(RAGState)) == _FROZEN_FIELDS


def _assert_lightweight(name: str, annotation: object) -> None:
    origin = get_origin(annotation)
    if annotation in _SCALARS:
        return
    if origin is Literal:
        assert all(isinstance(arg, str) for arg in get_args(annotation)), (
            f"{name}: Literal members must be strings"
        )
        return
    if origin is types.UnionType:
        for arg in get_args(annotation):
            _assert_lightweight(name, arg)
        return
    if origin is list:
        (member,) = get_args(annotation)
        if member is float:
            assert name in _FLOAT_LIST_ALLOWLIST, (
                f"{name}: a list[float] is an embedding vector unless proven otherwise "
                "(FR-PER-03 bans vectors from the checkpoint)"
            )
            return
        _assert_lightweight(name, member)
        return
    raise AssertionError(
        f"{name}: {annotation!r} is not a scalar, Literal, optional or list of scalars — "
        "FR-PER-03 keeps the checkpoint to ids and scalars"
    )


def test_rag_state_carries_no_heavy_payload() -> None:
    """FR-PER-03, enforced structurally rather than by review.

    Two independent checks, because either alone is defeatable: the *shape* check rejects
    dicts, bytes, dataclasses, pydantic models and `AnyMessage`; the *name* check rejects
    a `list[str]` called `chunk_texts`, which the shape check would happily accept.
    """
    for name, annotation in get_type_hints(RAGState).items():
        _assert_lightweight(name, annotation)
        if name.endswith(("_id", "_ids")):
            # A reference, which is precisely what FR-PER-03 sanctions holding — so
            # `user_message_id` is fine even though it contains "message".
            continue
        offending = sorted(part for part in _BANNED_NAME_PARTS if part in name)
        assert not offending, (
            f"`{name}` looks like it holds a payload ({offending}); FR-PER-03 "
            "keeps documents, chunk text, vectors and history out of the checkpoint"
        )


def test_state_module_does_not_import_langchain_core() -> None:
    """R-42(1) + §10.3, as a guard rather than a memory.

    An `add_messages` channel is the obvious thing for a later task to reach for, and it
    would (a) duplicate the transcript that OI-23 makes `messages` authoritative for,
    (b) re-serialise the whole history at every version bump, and (c) put an undeclared
    transitive package's class layout into our on-disk checkpoint format.
    """
    source = Path(__file__).resolve().parents[1] / "app" / "rag" / "state.py"
    text = source.read_text(encoding="utf-8")
    for needle in ("langchain_core", "add_messages", "AnyMessage"):
        assert f"import {needle}" not in text and f"{needle} import" not in text, (
            f"app/rag/state.py imports `{needle}` — conversation history is rehydrated "
            "from the `messages` table, never checkpointed (R-42(1))"
        )


def test_authorization_lives_in_the_context_never_in_the_state() -> None:
    """R-42(3) / NFR-SEC-06 / FR-RET-04.

    A resumed run must authorize from the live principal. If `owner_id` were checkpointed,
    a conversation would keep answering under the permissions captured when it started —
    across a role change, a deactivation, or a document being revoked.
    """
    state_fields = set(get_type_hints(RAGState))
    # `dataclasses.fields`, not `get_type_hints`: `RAGContext`'s annotations reference
    # `async_sessionmaker`, which is imported only under TYPE_CHECKING precisely so the
    # state module stays free of SQLAlchemy at runtime.
    context_fields = {f.name for f in dataclasses.fields(RAGContext)}
    for field in ("owner_id", "tenant_id", "is_admin"):
        assert field in context_fields
        assert field not in state_fields


def test_the_ambient_retrieval_scope_is_not_enumerated_in_state() -> None:
    """R-42(3), second half.

    Only explicit @-mentions (FR-ORC-06) are state. The ambient scope — "all GLOBAL
    documents visible to the user" — is a predicate T-305 applies in-query: enumerating it
    would be unbounded in size *and* would move an access-control decision into Python,
    which FR-RET-04 / NFR-SEC-06 forbid.
    """
    fields = set(get_type_hints(RAGState))
    assert "mentioned_document_ids" in fields
    assert not {"scope_document_ids", "visible_document_ids", "knowledge_base_ids"} & fields
