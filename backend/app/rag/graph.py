"""The orchestrator graph (T-301, §4.12, FR-ORC-01/07, R-42).

The §4.12 pseudocode is the flow of record and this module is its LangGraph form:
governance → telemetry start → UI lock → retrieval → generation → finalization, with the
FR-ORC-07 additions (injection screen before retrieval, query-adaptive routing, rerank,
groundedness gate, bounded retry, abstain, human review) as nodes on the same spine.

**T-301 ships the whole topology with stub node bodies.** T-302..T-308 fill the bodies in;
none of them needs to rewire an edge. The skeleton therefore runs end to end today, and
what it does is not a placeholder: with no retriever wired the retrieval scope is empty,
and R-23 / FR-SYS-02 / §4.12 precedence note (3) say exactly what must happen then — say
so and abstain, never answer from pre-training. `generate` raises on the *other* branch
rather than inventing text, so the skeleton cannot fabricate an answer even by accident.

Three structural invariants, each asserted in `tests/test_graph.py`:

1. **`finalize` is the only edge to `END`.** This is §4.12 precedence note (2) — "perform
   the finalization steps in a `finally` block" — expressed as reachability. Every
   terminal path (denial, injection block, abstain, exhausted retry, review, and any node
   exception via the default error handler) routes *through* the single node that unlocks,
   closes telemetry and updates stats, so a path that skips finalization is not something
   a reviewer has to notice: it cannot be drawn.
2. **No path from `START` to `retrieve` avoids `screen`** — FR-ORC-07's "screening node
   before retrieval" (NFR-SEC-05).
3. **`adapt` is the only node that writes `retry_count`, and the only edge back into
   `retrieve` comes from `adapt`.** Together those are FR-ORC-07's termination proof.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Literal

import structlog

from app.config import Settings, get_settings
from app.rag import telemetry
from app.rag.errors import (
    ACCESS_DENIED,
    ACCESS_DENIED_CODE,
    SYSTEM_FAILURE,
    classify,
    copy_for,
)
from app.rag.state import RAGContext, RAGState

# Must be set before langgraph's serde module is imported, because it snapshots the flag
# at import time (see `app.services.checkpointer` for the same call and the reasoning).
from app.services.checkpointer import apply_strict_msgpack
from app.services.processing_lock import (
    DatabaseProcessingLockStore,
    ProcessingLockStore,
    new_token,
)

apply_strict_msgpack()

from langgraph.errors import NodeError  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.runtime import Runtime  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

log = structlog.get_logger(__name__)

__all__ = [
    "ABSTAIN_EMPTY_SCOPE",
    "ACCESS_DENIED",
    "NODE_NAMES",
    "SYSTEM_FAILURE",
    "build_graph",
    "build_state_graph",
    "close_graph",
    "decide_after_gate",
    "get_graph",
    "thread_config",
]

# `ACCESS_DENIED` and `SYSTEM_FAILURE` now live in `app.rag.errors` beside the rest of the
# FR-ORC-05 copy, and are re-exported here so importers do not have to care which module
# owns a string. `errors` imports no langgraph — which is the point: T-402's chat route and
# T-505's error display need the copy without triggering `apply_strict_msgpack()`.

#: R-23 / FR-SYS-02: what the system says when the retrieval scope is empty. §8.4 tracks
#: "empty-retrieval-scope message copy (FR-SYS-02, R-23)" as an open copy TBD.
ABSTAIN_EMPTY_SCOPE = (  # TBD(§8.4)
    "I can't ground an answer to that — I couldn't find anything relevant in the "
    "documents available to you. Try uploading a relevant document, or rephrasing."
)

#: The full node set. Named for the FR-ORC-01 steps so traceability is a set comparison
#: rather than a reading exercise.
NODE_NAMES: tuple[str, ...] = (
    "govern",
    "telemetry_start",
    "lock",
    "screen",
    "route",
    "retrieve",
    "rerank",
    "generate",
    "gate",
    "adapt",
    "abstain",
    "review",
    "finalize",
)


# --- nodes --------------------------------------------------------------------
#
# Uniform signature: `(state, runtime) -> partial RAGState`. langgraph injects by
# parameter *name*, so `runtime` must be spelled exactly that.
#
# Every node that needs the database opens its own short-lived session from
# `runtime.context.sessionmaker`. Never a request-scoped one: a run outlives its HTTP
# request while streaming and has no request at all on resume, and holding a session
# across a multi-second generation call pins a pool slot for the whole turn — the failure
# R-41(7) already ruled on for the SSE stream.


async def govern(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 1 / FR-ORC-02 — authorize the request.

    Authorization is re-derived here from `runtime.context`, which is **not**
    checkpointed (R-42(3)). That is the whole point: a resumed run must answer under the
    caller's *current* permissions, not those captured in a checkpoint written before a
    role change, a deactivation, or the conversation changing hands.

    The two denial branches are **not** the same event. A foreign owner is a security
    event and is written to the NFR-SEC-08 trail; a conversation that no longer exists is
    a resumed run whose chat was deleted (R-42(11) / T-401), which is ordinary and is
    logged only. Auditing both would fill a security artefact with routine lifecycle noise.

    No telemetry span is opened on either path — `telemetry_start` is downstream, and a
    denial routes straight to `finalize`. `graph.turn.denied` stands in, so a span-pairing
    consumer never sees an end without a start (R-43(5)).
    """
    from app.db.models.conversation import Conversation
    from app.services.audit import record_authorization_denied

    ctx = runtime.context
    denial: RAGState = {
        "outcome": "blocked",
        "answer": ACCESS_DENIED,
        "error_code": ACCESS_DENIED_CODE,
    }

    async with ctx.sessionmaker() as session:
        conversation = await session.get(Conversation, ctx.conversation_id)

        if conversation is None:
            reason = "no_such_conversation"
        elif conversation.owner_id != ctx.owner_id and not ctx.is_admin:
            reason = "not_owner"
            try:
                await record_authorization_denied(
                    session,
                    actor_id=ctx.owner_id,
                    conversation_id=ctx.conversation_id,
                    turn_index=state.get("turn_index"),
                )
                await session.commit()
            except Exception:
                # The denial is enforced regardless. FR-ORC-02 fixes the response to
                # "Error: Access Denied", so letting an audit-write failure propagate would
                # turn a correctly refused turn into a reported system failure — a worse
                # answer to the user *and* a worse signal to the operator than a loud log.
                log.exception(
                    "graph.audit_write_failed",
                    conversation_id=str(ctx.conversation_id),
                    owner_id=str(ctx.owner_id),
                )
        else:
            return {}

    telemetry.turn_denied(conversation_id=ctx.conversation_id, owner_id=ctx.owner_id, reason=reason)
    return denial


async def telemetry_start(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 2 / FR-ORC-03 — open the telemetry span.

    `started_at` is **wall clock**, not `time.monotonic()`. A monotonic clock is the
    textbook choice for a duration and is the wrong one here: a checkpointed turn can span
    a process restart by construction (that is what the checkpointer is for), and a
    monotonic reading taken before the restart is meaningless after it.
    """
    telemetry.turn_start(
        conversation_id=runtime.context.conversation_id, turn_index=state.get("turn_index")
    )
    return {"started_at": time.time()}


def _lock_store(ctx: RAGContext, settings: Settings | None = None) -> ProcessingLockStore:
    """The injected gate, or the real database-backed one.

    Built per call rather than held on the context because it is a frozen dataclass over a
    sessionmaker — there is nothing to pool and nothing to close.
    """
    if ctx.processing_lock is not None:
        return ctx.processing_lock
    settings = settings or get_settings()
    return DatabaseProcessingLockStore(
        sessionmaker=ctx.sessionmaker, ttl_seconds=settings.graph.lock_ttl_seconds
    )


async def lock(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 3 / FR-ORC-04 — take the R-24 per-user action gate.

    R-24 rescoped FR-STA-02 to an *action-initiation* gate on the requesting user's
    mutating file operations, not a global KB freeze. This acquires it; `finalize` releases
    it on every path, which is what makes "the UI is always unlocked on completion, success
    or failure" structural rather than hopeful.

    Keyed on `ctx.owner_id` — the **requesting caller**, which is not
    `conversation.owner_id` when an admin runs someone else's chat. FR-STA-02 gates "the
    requesting user's GUI", so it is the admin's own KB actions that pause, not the
    conversation owner's.

    **Fails open.** A gate the store cannot publish is a degraded UX affordance; raising
    here would route through `handle_node_error` and turn it into a failed turn, which is a
    far worse outcome than an upload button that stays live for a few seconds. R-24 already
    places consistency on FR-ING-04/05 + FR-RET-04 and citation validity on serve-time
    FR-CIT-06, so nothing downstream depends on this succeeding.
    """
    ctx = runtime.context
    token = new_token()
    try:
        await _lock_store(ctx).acquire(
            owner_id=ctx.owner_id, conversation_id=ctx.conversation_id, token=token
        )
    except Exception:
        log.warning(
            "graph.lock_unavailable",
            conversation_id=str(ctx.conversation_id),
            owner_id=str(ctx.owner_id),
            exc_info=True,
        )
        return {"lock_token": None}
    return {"lock_token": token}


async def screen(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / NFR-SEC-05 — prompt-injection screening, *before* retrieval. T-303."""
    return {"injection_verdict": "clean"}


async def route(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-03 — query classification and strategy routing. T-304.

    `hybrid` is FR-RET-03's own default for an unclassified query, so the stub is the
    real fallback rather than a stand-in. T-304 must fan sub-queries out **inside**
    `retrieve` (`asyncio.gather`), never via `Send`: `Send` returns concurrent updates to
    one channel and would force merge reducers onto `retrieved_chunk_ids`, which R-42(4)
    rules out.
    """
    return {"strategy": "hybrid"}


async def retrieve(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 4 / FR-RET-01/04 — hybrid retrieval. T-305.

    Returns nothing until T-305 wires `HybridRetriever`. That is not a hole: an empty
    result is a legitimate state of the world (a user with no documents), and R-23 pins
    what happens next.
    """
    return {"retrieved_chunk_ids": []}


async def rerank(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-02 — cross-encoder rerank to the grounding top-K. T-306."""
    retrieved = state.get("retrieved_chunk_ids", [])
    return {"reranked_chunk_ids": list(retrieved), "rerank_scores": []}


async def generate(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 5 / FR-SYS-02 — grounded generation. T-307.

    The empty-grounding branch is complete and correct **today**: R-23 makes grounding
    always-on, so an empty scope abstains (FR-RET-05) instead of answering from
    pre-training, and §4.12 precedence note (3) says so explicitly.

    The other branch raises rather than returning something plausible. A stub that
    answered without a model would be indistinguishable from a working system in every
    test that did not read the text — precisely the failure NFR-VIS/FR-SYS-02 cannot
    tolerate in a grounded product.
    """
    if not state.get("reranked_chunk_ids"):
        return {
            "answer": ABSTAIN_EMPTY_SCOPE,
            "outcome": "abstained",
            "citation_ids": [],
            "gate_verdict": "pass",
        }
    raise NotImplementedError("generation is T-307")


async def gate(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-05 / FR-CIT-06 — groundedness + citation verification. T-308.

    NFR-PRF-02 is why this is a node and not a filter on the stream: the client sees
    nothing until this passes.
    """
    if state.get("outcome") == "abstained":
        return {"gate_verdict": "pass"}
    raise NotImplementedError("the groundedness gate is T-308")


async def adapt(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-RET-05 — modify the strategy and retry.

    **The only writer of `retry_count` and the only way back into `retrieve`.** Both
    halves matter: a second writer breaks the FR-ORC-07 bound, and a second back-edge
    would let a cycle run without incrementing it. T-308 chooses the new strategy; the
    increment is the part that guarantees termination and so belongs here from the start.
    """
    return {"retry_count": state.get("retry_count", 0) + 1}


async def abstain(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-RET-05 / R-23 — decline to answer.

    A terminal *node*, not an edge to `END`: an abstention is a response. It is persisted,
    telemetered and rendered like any other, so it must pass through `finalize`.
    """
    if state.get("injection_verdict") == "blocked":
        return {"outcome": "blocked", "answer": SYSTEM_FAILURE, "citation_ids": []}
    return {
        "outcome": "abstained",
        "answer": state.get("answer") or ABSTAIN_EMPTY_SCOPE,
        "citation_ids": [],
    }


async def review(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 — route to human review.

    Ships in T-301 rather than with T-308 because FR-PER-01 names "human-in-the-loop
    interruption state" among what the checkpointer must hold, and this node is the
    testable instance of that clause.

    The interrupt payload carries **ids only**, on the same FR-PER-03 rule as the state.
    Note that langgraph re-executes a node from the top on resume, so nothing here may
    have a side effect before the `interrupt` call.
    """
    decision = interrupt(
        {
            "reason": state.get("gate_reason"),
            "turn_index": state.get("turn_index"),
            "chunk_ids": state.get("reranked_chunk_ids", []),
        }
    )
    approved = bool(decision) and decision.get("decision") == "approve"
    if approved:
        return {"outcome": "answered", "gate_verdict": "pass"}
    return {"outcome": "review", "answer": state.get("answer") or ABSTAIN_EMPTY_SCOPE}


async def finalize(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 6 — the `finally` block: unlock, close telemetry, serve.

    Reached on **every** path, which is what §4.12 precedence note (2) asks for. T-302
    releases the R-24 lock and writes the telemetry end; T-402 persists the `messages`
    row and sets `answer_message_id`.

    Clearing `answer` once it is persisted is what makes FR-PER-03 an observable property
    rather than an intention: the state *at rest* — what the next turn loads — is then ids
    and scalars only, and the answer text exists in the checkpoint for the two supersteps
    between `generate` and here. It is cleared **only** when `answer_message_id` is set: an
    answer that has not been written anywhere else is the run's sole copy, and dropping it
    because the persist step failed would turn a recoverable error into lost work.
    **This node must never raise.** Its own error handler routes back to `finalize`, so an
    exception here would loop until the recursion limit rather than fail once. Both side
    effects are therefore guarded, and both guards are conditional on state rather than on
    swallowing everything:

    * The lock is released **only if this run holds a token**. `lock` never ran on the
      denial path, and it fails open on a store outage — so an unconditional
      release-by-owner would free a *concurrent* turn's gate.
    * Telemetry closes **only if a span was opened**. `started_at` is absent on the denial
      path, which skips `telemetry_start` entirely, and computing a latency from it would
      raise `TypeError` inside the one node R-42(5) makes structurally unskippable.
    """
    ctx = runtime.context
    update: RAGState = {"lock_token": None}
    if state.get("outcome") is None:
        update["outcome"] = "answered"
    if state.get("answer_message_id"):
        update["answer"] = None

    token = state.get("lock_token")
    if token:
        try:
            # A `False` return is normal: a later turn overwrote this one's gate, or it
            # expired and was re-taken. The token match is what stops us freeing that turn.
            await _lock_store(ctx).release(owner_id=ctx.owner_id, token=token)
        except Exception:
            log.warning(
                "graph.unlock_failed",
                conversation_id=str(ctx.conversation_id),
                exc_info=True,
            )

    started_at = state.get("started_at")
    if started_at is not None:
        latency_ms = int((time.time() - started_at) * 1000)
        outcome = update.get("outcome") or state.get("outcome")
        if outcome == "error":
            telemetry.turn_failure(
                conversation_id=ctx.conversation_id,
                turn_index=state.get("turn_index"),
                error_code=state.get("error_code"),
                latency_ms=latency_ms,
            )
        else:
            # `blocked` and `abstained` close as ends, not failures: an abstention is a
            # response (R-23), and reporting one as an incident would make every user with
            # an empty knowledge base look like an outage.
            telemetry.turn_end(
                conversation_id=ctx.conversation_id,
                turn_index=state.get("turn_index"),
                outcome=outcome,
                latency_ms=latency_ms,
                model_name=state.get("model_name"),
                prompt_tokens=state.get("prompt_tokens"),
                completion_tokens=state.get("completion_tokens"),
            )
    return update


async def handle_node_error(state: RAGState, error: NodeError) -> Command:
    """The §4.12 CATCH block (FR-ORC-05 / FR-ERR-04).

    Routes to `finalize` rather than to `END`, so the failure path unlocks and closes
    telemetry exactly like the success path — the pseudocode's `finally`. The exception
    itself is logged, never checkpointed: a traceback in a durable store leaks internals
    and is not an id or a scalar.

    `error_code` is a failure *class* (`app.rag.errors`), not `type(exc).__name__` as it
    was before T-302 — a Python class name is neither something FR-ERR-04 can attach copy
    to nor something a user can act on, and it put internal type names into a durable store.

    **Deliberately side-effect-free.** It is tempting to release the lock or write the audit
    row here; both would break an invariant. Release must happen in exactly one place or the
    token-matched single-release stops being a property of the topology and becomes a
    property of two code paths agreeing.
    """
    failure = classify(error.error)
    log.exception(
        "graph.node_failed",
        node=error.node,
        error=str(error.error),
        error_code=failure.value,
        detail_code=getattr(error.error, "code", None),
    )
    return Command(
        goto="finalize",
        update={
            "outcome": "error",
            "error_code": failure.value,
            "answer": copy_for(failure.value),
        },
    )


# --- routing ------------------------------------------------------------------


def decide_after_gate(
    state: RAGState, max_retries: int
) -> Literal["finalize", "adapt", "abstain", "review"]:
    """FR-ORC-07's conditional edge, as a pure function.

    Pure and parameterised so the termination argument can be tested without building a
    graph: `retry` is honoured only while `retry_count` is *under* the bound, and every
    other outcome — including an exhausted retry budget — lands on a terminal node. There
    is no verdict that loops without passing through `adapt`.
    """
    verdict = state.get("gate_verdict")
    if verdict == "pass":
        return "finalize"
    if verdict == "review":
        return "review"
    if verdict == "retry" and state.get("retry_count", 0) < max_retries:
        return "adapt"
    return "abstain"


def _route_after_govern(state: RAGState) -> Literal["telemetry_start", "finalize"]:
    """FR-ORC-02 — a denial stops processing, but still finalizes."""
    return "finalize" if state.get("outcome") == "blocked" else "telemetry_start"


def _route_after_screen(state: RAGState) -> Literal["route", "abstain"]:
    """NFR-SEC-05 — a blocked prompt never reaches retrieval or the model."""
    return "abstain" if state.get("injection_verdict") == "blocked" else "route"


def _route_after_gate(state: RAGState) -> Literal["finalize", "adapt", "abstain", "review"]:
    return decide_after_gate(state, get_settings().graph.max_retries)


# --- construction -------------------------------------------------------------


def build_state_graph(settings: Settings | None = None) -> StateGraph:
    """Build the uncompiled graph. Pure — no I/O, no connections, no settings reads.

    Returns a **fresh** builder every call, deliberately: `compile()` mutates the builder
    (it appends the default error-handler node), so a shared one could neither be compiled
    twice nor inspected afterwards for its own node set.
    """
    settings = settings or get_settings()
    builder: StateGraph = StateGraph(RAGState, context_schema=RAGContext)

    # FR-ORC-05: any node that raises lands in `finalize` with a failure class instead of
    # killing the run — the pseudocode's CATCH, applied uniformly rather than per node.
    builder.set_node_defaults(
        error_handler=handle_node_error,
        timeout=settings.graph.node_timeout_seconds,
    )

    builder.add_node("govern", govern)
    builder.add_node("telemetry_start", telemetry_start)
    builder.add_node("lock", lock)
    builder.add_node("screen", screen)
    builder.add_node("route", route)
    builder.add_node("retrieve", retrieve)
    builder.add_node("rerank", rerank)
    builder.add_node("generate", generate)
    builder.add_node("gate", gate)
    builder.add_node("adapt", adapt)
    builder.add_node("abstain", abstain)
    builder.add_node("review", review)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "govern")
    builder.add_conditional_edges(
        "govern",
        _route_after_govern,
        {"telemetry_start": "telemetry_start", "finalize": "finalize"},
    )
    builder.add_edge("telemetry_start", "lock")
    builder.add_edge("lock", "screen")
    builder.add_conditional_edges(
        "screen", _route_after_screen, {"route": "route", "abstain": "abstain"}
    )
    builder.add_edge("route", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", "gate")
    builder.add_conditional_edges(
        "gate",
        _route_after_gate,
        {
            "finalize": "finalize",
            "adapt": "adapt",
            "abstain": "abstain",
            "review": "review",
        },
    )
    # The single back-edge, from the single node that increments `retry_count`.
    builder.add_edge("adapt", "retrieve")
    builder.add_edge("abstain", "finalize")
    builder.add_edge("review", "finalize")
    builder.add_edge("finalize", END)
    return builder


def build_graph(saver: BaseCheckpointSaver, settings: Settings | None = None):  # noqa: ANN201
    """Compile the graph against a checkpointer. One compiled graph serves the process."""
    return build_state_graph(settings).compile(checkpointer=saver)


def thread_config(conversation_id: uuid.UUID | str, settings: Settings | None = None) -> dict:
    """The langgraph `config` for one conversation — FR-PER-02's `thread_id` in one place.

    `thread_id` *is* the conversation id (same UUID), so every caller building this by
    hand is a chance to drift; there is one builder instead. `recursion_limit` rides along
    because it is a correctness bound tied to `GRAPH_MAX_RETRIES`, not a per-call taste.
    """
    settings = settings or get_settings()
    return {
        "configurable": {"thread_id": str(conversation_id)},
        "recursion_limit": settings.graph.recursion_limit,
    }


# --- factory / DI -------------------------------------------------------------

_graph: CompiledStateGraph | None = None
_graph_lock = asyncio.Lock()


async def get_graph():  # noqa: ANN201 — CompiledStateGraph is generic over four params
    """The process-wide compiled graph, built on first use.

    Lazy, like every other client in this codebase: a cold Postgres must not stop the API
    from booting (`app.main.lifespan` opens nothing). The compiled graph is immutable and
    everything per-run arrives through `context=` and `config=`, so one instance serves
    every conversation.
    """
    global _graph
    if _graph is not None:
        return _graph
    async with _graph_lock:
        if _graph is None:
            from app.services.checkpointer import get_checkpointer

            saver = await get_checkpointer().get()
            _graph = build_graph(saver)
    return _graph


async def close_graph() -> None:
    """Forget the compiled graph (app lifespan / test teardown).

    The saver it closes over is owned and released by `app.services.checkpointer`; this
    only drops the reference, so `close_checkpointer()` must be called too.
    """
    global _graph
    _graph = None
