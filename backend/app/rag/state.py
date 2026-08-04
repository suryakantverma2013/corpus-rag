"""The LangGraph state contract (T-301, FR-PER-01..03, R-42).

Two schemas that must not be confused:

* :class:`RAGState` is **checkpointed**. Every field survives a process restart and is
  read back by the next turn of the conversation. It holds **ids and scalars only**.
* :class:`RAGContext` is langgraph's ``context_schema`` — run-scoped dependencies that
  are **never** checkpointed. Sessions, clients and the caller's identity live here.

**Conversation history is not in either.** FR-PER-01 lists "message history used by the
graph" among what the checkpointer stores; R-42(1) satisfies that *by reference*: the
state carries ``user_message_id``/``turn_index`` and the thread's history is
``messages WHERE conversation_id = thread_id ORDER BY seq``, which OI-23 already makes
the authoritative copy. Three independent reasons, any one of which would be enough:

1. FR-PER-03 forbids "duplicated per-turn context". A langgraph channel is re-serialised
   *in full* every time it changes, so an ``add_messages`` history would cost
   ``O(turns × history_bytes × supersteps)`` in `checkpoint_blobs` — and R-30 sends the
   full untruncated history, so it only grows.
2. Two authoritative transcripts drift. FR-MSG-08 Regenerate replaces
   ``messages.content``; a message channel would keep the superseded answer and feed it
   to the model forever, so the user would be talking to a transcript they cannot see.
3. ``add_messages``/``AnyMessage`` are `langchain_core`, which is transitive and
   undeclared (§10.3). Storing its classes would make an undeclared package's layout part
   of our on-disk format, and there is no consumer anyway — generation calls the OpenAI
   SDK directly (R-15), so messages would be flattened to dicts at the boundary.

The field list below **is a contract binding T-302..T-310** (R-42(2)). Adding an optional
key is safe — an old checkpoint simply has no value for the new channel. **Renaming,
retyping or removing one is breaking**: it orphans conversations that are mid-turn when
the deploy lands. Any such change needs a note on the changing task's board line and an
edit to the frozen field-name set in `tests/test_graph.py`, in the same commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

from app.rag.prefetch import QueryArmPrefetch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.rag.retrieval import Retriever
    from app.services.embeddings import EmbeddingClient
    from app.services.llm import ChatClient
    from app.services.processing_lock import ProcessingLockStore

__all__ = [
    "GateVerdict",
    "InjectionVerdict",
    "Outcome",
    "QueryClass",
    "RAGContext",
    "RAGState",
    "Strategy",
    "fresh_turn_state",
]

#: FR-RET-03 retrieval strategies. `graph` falls back to hybrid until GraphRAG ships (R-21).
Strategy = Literal["hybrid", "refine", "decompose", "hyde", "graph"]

#: FR-RET-03 query classes the router (T-304) assigns.
QueryClass = Literal["simple", "vague", "multi_part", "semantic_gap", "relationship"]

#: NFR-SEC-05 prompt-injection screen verdicts (T-303).
InjectionVerdict = Literal["clean", "suspicious", "blocked"]

#: FR-ORC-07 groundedness/citation gate verdicts (T-308). `retry` is honoured only while
#: `retry_count` is under GRAPH_MAX_RETRIES; past that it degrades to `abstain`.
GateVerdict = Literal["pass", "retry", "abstain", "review"]

#: How the turn ended. `abstained` is a real answer (R-23), not a failure.
Outcome = Literal["answered", "abstained", "blocked", "error", "review"]


class RAGState(TypedDict, total=False):
    """Checkpointed per-conversation graph state (FR-PER-01..03, R-42(2)).

    **IDS AND SCALARS ONLY.** A field holding document text, chunk text, embedding
    vectors, images or conversation history breaks FR-PER-03 and is rejected by
    `tests/test_graph.py::test_rag_state_carries_no_heavy_payload`.

    UUIDs are `str`, not `uuid.UUID`: that matches `thread_id`'s own type, survives any
    future change of checkpoint serializer, and keeps the "scalars only" guard trivially
    decidable.

    Every channel is a plain `LastValue` — there are **no reducers**, deliberately
    (R-42(4)). `operator.add` on `retrieved_chunk_ids` would make a retry ground on the
    *union* of both attempts, which is the opposite of FR-RET-05's "retry with a modified
    strategy"; and an additive `retry_count` would double-count the moment a second node
    wrote it, breaking FR-ORC-07's termination guarantee in a way no obvious test catches.
    """

    # --- turn input (written once by the caller: T-402 send / T-404 regenerate) ---
    #: The current question, verbatim. Text, and correctly so: it is the run's *input*,
    #: not duplicated context, it is bounded by FR-STA-04's warn-and-block, and without it
    #: a resumed run cannot redo the turn.
    query: str
    #: `messages.id` of the row this turn answers — the FR-PER-04 handle back to app data.
    user_message_id: str
    #: Monotone per thread. Telemetry (FR-ORC-03) and resume diagnostics.
    turn_index: int
    #: FR-ORC-06 **explicit @-mentions only**. The ambient scope (the owner's GLOBAL KB +
    #: this conversation's KB) is deliberately absent: it is a *predicate* T-305 applies
    #: in-query from `RAGContext`, and enumerating it here would both bloat the checkpoint
    #: without bound and move an access-control decision into Python, which FR-RET-04 /
    #: NFR-SEC-06 forbid.
    mentioned_document_ids: list[str]

    # --- §4.12 prologue (T-302) ---
    #: Wall clock, opened by `telemetry_start`. Optional since T-406: the per-turn reset must be
    #: able to express "no span yet", and a `0.0` sentinel would satisfy `finalize`'s
    #: `is not None` guard and close a span the denial path never opened (R-43(5) pairing).
    started_at: float | None
    #: Opaque handle for the R-24 per-user action gate; released by `finalize`.
    lock_token: str | None

    # --- prompt-injection screen (T-303, NFR-SEC-05) ---
    injection_verdict: InjectionVerdict | None
    #: Short rule code. Never the offending text — that is the payload, and checkpointing
    #: it would persist attacker-chosen content verbatim.
    injection_rule: str | None

    # --- query-adaptive routing (T-304, FR-RET-03) ---
    query_class: QueryClass | None
    strategy: Strategy
    #: Decomposition sub-queries. Short and bounded; no reducer, because T-304 fans out
    #: *inside* the retrieve node rather than via `Send` (R-42(4)).
    sub_queries: list[str]

    # --- retrieval + rerank (T-305 / T-306) ---
    #: Named verbatim by FR-PER-03 as the example of what state may hold.
    retrieved_chunk_ids: list[str]
    #: Ordered top-K — this *is* the grounding context, by reference.
    reranked_chunk_ids: list[str]
    #: Positionally aligned with `reranked_chunk_ids`; the FR-CIT-04 displayed score.
    rerank_scores: list[float]

    # --- generation (T-307) ---
    #: The generated answer. Unavoidable and correct: NFR-PRF-02 requires the answer be
    #: complete and gated *before* a byte streams, so it must cross generate → gate →
    #: finalize. `finalize` then clears it (see R-42(2)), so the state **at rest** — what a
    #: follow-up turn loads — carries no answer text.
    answer: str | None
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None

    # --- groundedness / citation gate (T-308, FR-RET-05, FR-CIT-06) ---
    groundedness: float | None
    gate_verdict: GateVerdict | None
    #: A code, not prose.
    gate_reason: str | None
    #: Named verbatim by FR-PER-03. The FR-MSG-06 citation payload (which embeds a quoted
    #: passage) is assembled at finalization and written to `messages.citations`; it never
    #: enters state.
    citation_ids: list[str]
    #: FR-ORC-07's termination bound. Written by exactly one node (`adapt`).
    retry_count: int

    # --- finalization (T-302, FR-ORC-05 / FR-ERR-04) ---
    outcome: Outcome | None
    #: FR-ERR-04 failure class. A code — never a traceback, which would leak internals into
    #: a durable store.
    error_code: str | None
    #: Set once the answer's `messages` row is committed. The app writes through asyncpg and
    #: the checkpointer through psycopg, so the two can never be atomic — this field is what
    #: makes `finalize` idempotent on resume instead of writing a second answer row.
    answer_message_id: str | None


def fresh_turn_state() -> RAGState:
    """Every channel at its start-of-turn value (T-406). Spread into a run's input.

    **A thread is a checkpoint lineage, not a variable scope.** `thread_config` gives a
    conversation one `thread_id` for its whole life (FR-PER-02) and every channel above is a
    plain `LastValue` with no reducer (R-42(4)), so anything the input does not seed is *last
    turn's value*, silently. That is not a corner case: it made `finalize` skip its INSERT and
    serve turn 1's row as turn 2's answer, made a single errored turn suppress persistence for
    the life of the chat, and made an abstention inherit the previous turn's `model_name` and
    token counts.

    **Every field here is per-turn — there is no conversation-scoped channel**, by ruling and
    not by accident: conversation state lives in `messages` (R-42(1), OI-23) and the caller's
    identity in the never-checkpointed `RAGContext` (R-42(3)). So the correct reset is *all of
    them*, and a new channel that does not appear below is a carry-over waiting to ship —
    which is why `tests/test_graph.py` asserts this dict against `RAGState` and against the
    frozen field set, and asserts that `run_turn` still seeds every channel.

    The reset belongs in the **input**, not in a node at `START`. The tempting reason — "a
    START node would re-run on resume and wipe a run parked on `interrupt()`" — was *measured
    and is false*: a resume passes `None`, langgraph re-enters the pending task and never
    re-runs the prologue, and `test_checkpointer.py`'s resume test stays green against a
    prototype that put the reset in a node. The real reasons are two, and both are about drift:
    such a node must read the turn's *input* fields out of the state it is resetting and write
    them back, so the input list becomes a second hand-maintained copy of the contract that
    grows with every field a driver seeds (T-404 adds one); and nothing can then check the
    driver, where the input reset makes `test_run_turn_seeds_every_channel` possible. It also
    costs a superstep and a checkpoint write per turn for something the input expresses free.
    """
    return {
        "query": "",
        "user_message_id": "",
        "turn_index": 0,
        "mentioned_document_ids": [],
        "started_at": None,
        "lock_token": None,
        "injection_verdict": None,
        "injection_rule": None,
        "query_class": None,
        # FR-RET-03's own default, and the value `route` fails open to (R-45(2)).
        "strategy": "hybrid",
        "sub_queries": [],
        "retrieved_chunk_ids": [],
        "reranked_chunk_ids": [],
        "rerank_scores": [],
        "answer": None,
        "model_name": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "groundedness": None,
        "gate_verdict": None,
        "gate_reason": None,
        "citation_ids": [],
        "retry_count": 0,
        "outcome": None,
        "error_code": None,
        "answer_message_id": None,
    }


@dataclass(frozen=True, slots=True)
class RAGContext:
    """Run-scoped dependencies. **Never checkpointed** (langgraph ``context_schema``).

    Authorization lives here and *only* here (R-42(3)). A resumed run must authorize from
    the live principal, never from one persisted alongside the conversation: a checkpoint
    written before a role change, a deactivation or a document being revoked would
    otherwise keep answering under the permissions of the past (NFR-SEC-06, FR-RET-04).

    ``sessionmaker`` rather than a session: a graph run outlives its HTTP request under
    streaming and has no request at all on resume, so each node opens its own short-lived
    session. Holding one for the whole turn would pin a pool slot across a multi-second
    generation call — the failure R-41(7) already ruled on for the SSE stream.
    """

    owner_id: uuid.UUID
    tenant_id: uuid.UUID
    #: == `thread_id` (FR-PER-02). The same UUID as `conversations.id`.
    conversation_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]
    #: T-305 embeds the query itself — retrievers take text + vector and never embed
    #: (R-37(10)).
    embeddings: EmbeddingClient | None = None
    #: Injected so T-305 can be tested against a fake and so OI-18's store swap stays
    #: configuration-shaped.
    retriever_factory: Callable[[AsyncSession], Retriever] | None = field(default=None)
    #: The R-24 gate `lock` takes and `finalize` releases (T-302, R-43). Injected for the
    #: same reason as `retriever_factory` — it is the only way to reach the in-memory
    #: double, which is therefore unreachable from a deployment. `None` means "build the
    #: database-backed one from `sessionmaker`", which is what every real run does.
    processing_lock: ProcessingLockStore | None = None
    #: The chat-completions seam (T-304's router; T-307's generator). Injected for the same
    #: reason as the two above — it is the only route to `FakeChatClient`, so the double stays
    #: unreachable from a deployment. `None` means "build the one `LLM_BACKEND` names", which
    #: is what every real run does.
    chat: ChatClient | None = None
    #: The T-311 query-arm handoff: `route` starts the original query's retrieval arm
    #: concurrently with the router call and leaves the hits here for `retrieve`. It lives on
    #: the context rather than in `RAGState` because it carries chunk *objects*, which
    #: FR-PER-03 keeps out of the checkpoint — and because it must not survive a restart:
    #: an empty slot simply means `retrieve` searches the query itself, exactly as it did
    #: before T-311. Default-constructed, so no caller has to know it exists.
    prefetch: QueryArmPrefetch = field(default_factory=QueryArmPrefetch)
    is_admin: bool = False
