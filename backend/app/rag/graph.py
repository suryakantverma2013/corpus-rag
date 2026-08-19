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
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal

import structlog

from app.config import Settings, get_settings
from app.db.repositories.turn_telemetry import TurnTelemetryRepository
from app.rag import telemetry
from app.rag.errors import (
    ABSTAIN_LOW_GROUNDEDNESS,
    ACCESS_DENIED,
    ACCESS_DENIED_CODE,
    BLOCKED_INJECTION,
    SYSTEM_FAILURE,
    classify,
    copy_for,
)
from app.rag.retrieval import RetrievalFilter
from app.rag.state import GateVerdict, RAGContext, RAGState

# Must be set before langgraph's serde module is imported, because it snapshots the flag
# at import time (see `app.services.checkpointer` for the same call and the reasoning).
from app.services.checkpointer import apply_strict_msgpack
from app.services.model_selection import ModelSlot
from app.services.processing_lock import (
    DatabaseProcessingLockStore,
    ProcessingLockStore,
    new_token,
)
from app.tracing import record_turn_span

apply_strict_msgpack()

from langgraph.errors import NodeError  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.runtime import Runtime  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.rag.retrieval import Retriever
    from app.services.embeddings import EmbeddingClient

log = structlog.get_logger(__name__)

__all__ = [
    "ABSTAIN_EMPTY_SCOPE",
    "ABSTAIN_LOW_GROUNDEDNESS",
    "ACCESS_DENIED",
    "BLOCKED_INJECTION",
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

#: R-23 / FR-SYS-02: what the system says when the retrieval scope is empty.
#: Author-confirmed 2026-08-18 (R-86(2)) and written into FR-SYS-02, so this derives from
#: the requirement rather than standing in for a decision nobody had taken. It says what
#: happened, and then what to do about it — R-67(2)'s shape, where naming the escape is
#: what stops a user reading a bounded answer as a broken product.
ABSTAIN_EMPTY_SCOPE = (
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
    """FR-ORC-07 / NFR-SEC-05 — prompt-injection screening, *before* retrieval (T-303, R-44).

    Screens **the query only** (R-44(1)). Retrieved document text is not screened here and is
    never blocked: it is neutralised and fenced by `app.rag.prompts`, which is where NFR-SEC-05's
    "treated as data, never as system instructions" actually lives. Blocking on chunk text would
    be a denial-of-service against the caller's own knowledge base — one uploaded security policy
    containing "ignore previous instructions" would make every query that retrieves it
    permanently unanswerable — and the chunk is already inside the caller's FR-RET-04 scope, so
    there is no privilege boundary a block would be defending.

    **Fails closed**, unlike `lock`. This node has no `try`, so a screening error routes through
    `handle_node_error` to `finalize` with no retrieval and no generation. That is deliberate and
    is the R-32 direction ("unreachable scanner → job fails closed"): the gate is advisory and
    degrades safely, a screen is not. It is also why nothing here does I/O — the rules are
    compiled at import, so the only way to reach that path is a bug.

    `suspicious` proceeds to retrieval and changes nothing downstream (R-44(4)). Its purpose is
    to keep the block tier narrow: Corpus is document QA, so a user asking *about* injection is a
    real question, and the tier is what lets a lower-precision rule report without refusing.
    Appending "be careful" to the prompt on a suspicious turn was considered and rejected — an
    instruction to an already-compromised channel is a suggestion, not a control.
    """
    from app.security.prompt_injection import screen_query

    ctx = runtime.context
    if not get_settings().graph.screen_enabled:
        # Diagnosis path only (see `GraphSettings.screen_enabled`). Logged at warning so a box
        # left in this state is visible rather than quietly unscreened.
        log.warning("security.injection.screen_disabled", conversation_id=str(ctx.conversation_id))
        return {"injection_verdict": "clean", "injection_rule": None}

    result = screen_query(state.get("query", ""))
    if result.verdict != "clean":
        # Named outside the closed `graph.turn.*` vocabulary so R-43(5)'s span-pairing contract
        # and `telemetry.EVENT_NAMES` are untouched — this is a security event, not a turn
        # boundary. The rule *code* only: R-43(5)'s "no payload text ever" applies with extra
        # force here, since the text in question is attacker-chosen.
        log.warning(
            f"security.injection.{result.verdict}",
            conversation_id=str(ctx.conversation_id),
            owner_id=str(ctx.owner_id),
            turn_index=state.get("turn_index"),
            rule=result.rule,
        )
    return {"injection_verdict": result.verdict, "injection_rule": result.rule}


async def route(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-03 — query classification and strategy routing (T-304, R-45).

    One schema-constrained call on the cheap `OPENAI_ROUTER_MODEL` classifies the query and
    returns the derived probes; `app.rag.router` owns the prompt, the closed class set and
    FR-RET-03's class→strategy mapping.

    **Fails open, and that is the point** (R-45(2)). Unlike `screen` — which has no `try`
    because a security control must fail closed — everything here is wrapped, because
    `hybrid` with no probes is exactly what FR-RET-03 prescribes for an unclassified query.
    Letting a retrieval-quality optimisation turn a healthy turn into `SYSTEM_FAILURE` would
    make the product worse than not having the optimisation at all. The history read is inside
    the guard for the same reason: a router that cannot see the tail classifies worse, it does
    not fail.

    `sub_queries` carries **derived probes only** (R-45(3)) — T-305 always retrieves with
    `query` itself as well, so an empty list means "just the query" and a bad rewrite can
    never remove the original signal. T-305 must fan those out **inside** `retrieve`
    (`asyncio.gather`), never via `Send`: `Send` returns concurrent updates to one channel and
    would force merge reducers onto `retrieved_chunk_ids`, which R-42(4) rules out.

    **That same additivity is why this node also starts retrieving** (T-311). The original
    query is searched on every turn whatever the router concludes, so its arm depends on
    nothing computed here and there is no reason for it to wait behind a ~840 ms model call.
    It runs under the same `gather` and leaves its hits in `ctx.prefetch` for `retrieve`.
    Four properties hold it in place:

    * **Neither coroutine can cancel the other.** Both swallow their own exceptions — this
      one to `FALLBACK`, the arm into its slot — so `gather` never sees a raise, and a router
      failure (R-45(2)) cannot take the retrieval down with it.
    * **The failure direction is unchanged.** A prefetched arm that failed is re-raised by
      `retrieve`, which fails closed (R-46(6)); nothing about that decision moves here.
    * **The scope is not rebuilt.** `_retrieval_filter` is the same helper `retrieve` and
      `rerank` call — a second construction is how R-46(2)'s hole reopens (R-47(5)).
    * **The screen still precedes retrieval.** `_route_after_screen` sends a blocked prompt
      to `abstain` and never here, so FR-ORC-07's ordering is still structural: this node is
      unreachable on the branch where retrieval must not happen.
    """
    from app.rag.history import load_router_tail
    from app.rag.router import FALLBACK, RouterDecision, classify_query
    from app.rag.search import prefetch_query_arm

    ctx = runtime.context
    settings = get_settings()
    query = state.get("query", "")

    if not settings.graph.router_enabled:
        # Deliberate "stop paying for the extra call", not a failure path — hence info, where
        # the screen's kill switch logs a warning. Turning this off removes an improvement;
        # turning that one off removes a control.
        #
        # No prefetch on this path either, which is what keeps the flag a *pure* cost switch:
        # with no model call to hide behind there is nothing to overlap, and starting the arm
        # a superstep early would only move the same work between two nodes.
        log.info("rag.router.disabled", conversation_id=str(ctx.conversation_id))
        return {"query_class": None, "strategy": FALLBACK.strategy, "sub_queries": []}

    async def _classify() -> RouterDecision:
        try:
            history = []
            if settings.router.history_turns > 0:
                async with ctx.sessionmaker() as session:
                    history = await load_router_tail(
                        session,
                        ctx.conversation_id,
                        turns=settings.router.history_turns,
                        max_chars=settings.router.history_max_chars,
                    )
            chat = ctx.chat
            if chat is None:
                from app.services.llm import get_chat_client

                chat = get_chat_client()
            return await classify_query(
                query=query,
                chat=chat,
                history=history,
                settings=settings,
                model=_model_for(ctx, ModelSlot.ROUTER),
            )
        except Exception:
            log.warning(
                "rag.router.failed",
                conversation_id=str(ctx.conversation_id),
                turn_index=state.get("turn_index"),
                exc_info=True,
            )
            return FALLBACK

    async def _prefetch() -> None:
        if not settings.retrieval.prefetch_query_arm:
            return
        filters = _retrieval_filter(state, ctx)
        if filters is None:
            # Every `@`-mention was malformed, so the scope is empty by construction and
            # `retrieve` will return `[]` without searching. Prefetching here would be the
            # one search R-46(1) exists to prevent.
            return
        embeddings, retriever_factory = _retrieval_deps(ctx)
        await prefetch_query_arm(
            ctx.prefetch,
            query=query,
            filters=filters,
            sessionmaker=ctx.sessionmaker,
            embeddings=embeddings,
            embedding_model=_model_for(ctx, ModelSlot.EMBEDDING),
            retriever_factory=retriever_factory,
            settings=settings,
        )

    decision, _ = await asyncio.gather(_classify(), _prefetch())

    return {
        "query_class": decision.query_class,
        "strategy": decision.strategy,
        "sub_queries": list(decision.sub_queries),
    }


def _mentioned_document_ids(state: RAGState) -> tuple[list[uuid.UUID], bool]:
    """Parse `mentioned_document_ids`; report whether the turn carried any at all.

    The second half of the return is what makes the R-46(1) narrowing safe. Mentions
    *restrict* the scope, so "the turn had mentions but none of them parsed" and "the turn
    had no mentions" must not collapse into the same filter: the first would silently widen
    retrieval back to the whole ambient scope and answer from documents the user did not
    ask about. A malformed id is dropped rather than raised on — it is a client bug, and
    failing the turn over one bad entry in a list of four is a worse answer than honouring
    the three that parsed.
    """
    raw = state.get("mentioned_document_ids") or []
    parsed: list[uuid.UUID] = []
    for value in raw:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError, AttributeError, TypeError:
            log.warning("graph.mention_unparseable")
    return parsed, bool(raw)


def _retrieval_filter(state: RAGState, ctx: RAGContext) -> RetrievalFilter | None:
    """The FR-ORC-06 scope for this turn, or ``None`` when it is empty by construction.

    Shared by `retrieve` and `rerank` **because they must never differ**. T-306 re-reads the
    chunk rows it reranks (`RAGState` carries ids only), and the one way that read could
    reintroduce the hole R-46(2) closed is by rebuilding the scope slightly differently —
    an omitted `conversation_id` here would let the reranker read, score and ground in a
    chunk from another chat that retrieval was never allowed to return.

    ``None`` means "the turn asked for a document set that resolves to nothing" (every
    `@`-mention malformed), which is not the same as "no mentions" and must not collapse into
    it — see :func:`_mentioned_document_ids`.
    """
    document_ids, had_mentions = _mentioned_document_ids(state)
    if had_mentions and not document_ids:
        return None
    return RetrievalFilter(
        # The requesting caller, as `lock` is keyed (R-46(7), OI-33) — never the
        # conversation's owner, which differs on the FR-ORC-02 `is_admin` path.
        owner_id=ctx.owner_id,
        tenant_id=ctx.tenant_id,
        conversation_id=ctx.conversation_id,
        document_ids=document_ids,
    )


def _retrieval_deps(ctx: RAGContext) -> tuple[EmbeddingClient, Callable[[AsyncSession], Retriever]]:
    """The embedding client and retriever factory a search runs on.

    Shared by `retrieve` and by T-311's prefetch in `route`, for the reason `_retrieval_filter`
    is shared by `retrieve` and `rerank`: the arm started early and the arms started late must
    be the same query against the same store, and two builders is how one node quietly starts
    searching something the other would not.

    `HybridRetriever` is FR-RET-01's committed default when nothing is injected;
    `PgVectorRetriever` stays reachable through the factory as the sanctioned dense-only
    fallback.
    """
    from app.db.repositories.retrieval import HybridRetriever

    embeddings = ctx.embeddings
    if embeddings is None:
        from app.services.embeddings import get_embedding_client

        embeddings = get_embedding_client()
    return embeddings, ctx.retriever_factory or HybridRetriever


def _model_for(ctx: RAGContext, slot: ModelSlot) -> str | None:
    """The model id this turn calls for ``slot``, or ``None`` for the configured default.

    A :class:`ModelSlot` member rather than a string, deliberately: a mistyped attribute
    name would raise inside `route` or `rerank`, both of which fail open (R-45(2), R-47(2)),
    so the bug would surface as a permanently degraded stage rather than as an error. An
    enum member is wrong at import time instead.
    """
    return None if ctx.models is None else ctx.models.for_slot(slot)


def _message_uuid(value: str | None) -> uuid.UUID | None:
    """`RAGState.user_message_id` as a UUID, or ``None``.

    ``None`` on a malformed value rather than a raise: the history read is a quality input,
    not a correctness one, and the cost of getting it wrong here is bounded — the repository
    answers an unknown id with an empty transcript, never with another conversation's.
    """
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError, AttributeError, TypeError:  # pragma: no cover - we wrote this id
        log.warning("graph.user_message_id_unparseable")
        return None


def _parse_chunk_ids(values: Sequence[str]) -> list[uuid.UUID]:
    """Checkpointed chunk ids back to UUIDs, dropping anything malformed.

    Shared by `rerank` and `generate`: both re-read the rows a previous node listed, and both
    read ids that this graph wrote itself — so a failure here is a bug, not input validation,
    and dropping the id degrades to "that passage is gone", which both nodes already handle.
    """
    parsed: list[uuid.UUID] = []
    for value in values:
        try:
            parsed.append(uuid.UUID(value))
        except ValueError, AttributeError, TypeError:  # pragma: no cover - we wrote these ids
            log.warning("graph.chunk_id_unparseable")
    return parsed


async def retrieve(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 4 / FR-RET-01/04/FR-ORC-06 — hybrid retrieval (T-305, R-46).

    The FR-ORC-06 scope — "the active chat's attachments + all GLOBAL documents visible to
    the user + any explicitly `@`-mentioned documents" — is assembled here and applied
    **entirely inside the query** (FR-RET-04 / NFR-SEC-06). Two halves:

    * The ambient half is `RetrievalFilter.conversation_id`, which
      `app.db.repositories.retrieval` turns into a knowledge-base predicate. It is derived
      from `runtime.context`, which is not checkpointed (R-42(3)), so a resumed run scopes
      to the caller's *current* identity rather than to one captured in a checkpoint.
    * The explicit half is `mentioned_document_ids`, which **narrows** (R-46(1)). It is
      AND-ed with the ambient predicate rather than unioned with it, so a mention naming a
      document outside the caller's scope retrieves nothing instead of reaching it.

    Scoped on `ctx.owner_id` — the requesting caller, as `lock` is. An admin running someone
    else's conversation therefore grounds in their **own** documents (**OI-33**): the
    alternative exposes one user's documents to another and nothing in the spec asks for it.

    **Fails closed, like `screen` and unlike `route`.** No `try`: FR-RET-05 says in as many
    words that an unavailable vector store returns a graceful error and does not fabricate,
    and `handle_node_error` classifies these as `RETRIEVAL_UNAVAILABLE`. The one degradation
    is inside `retrieve_for_turn` — a *derived* probe may fail without failing the turn,
    because R-45(3) makes probes additive to a query that is always searched itself.

    **The query's arm may already have run** (T-311): `route` starts it concurrently with the
    classification call and leaves the hits in `ctx.prefetch`, which `retrieve_for_turn`
    consumes if it was filled for this probe under this scope. That changes when the search
    happened and nothing else — not the scope, not the merge, and not the direction this node
    fails in, since a prefetched arm that raised is re-raised here.
    """
    from app.rag.search import retrieve_for_turn

    ctx = runtime.context
    filters = _retrieval_filter(state, ctx)
    if filters is None:
        # Every mention was malformed. Under R-46(1) the scope they asked for is empty, so
        # this retrieves nothing and R-23 abstains — the one outcome that is neither a lie
        # nor an answer about the wrong documents.
        log.warning("graph.mentions_all_unparseable", conversation_id=str(ctx.conversation_id))
        return {"retrieved_chunk_ids": []}

    embeddings, retriever_factory = _retrieval_deps(ctx)
    hits = await retrieve_for_turn(
        query=state.get("query", ""),
        sub_queries=state.get("sub_queries", []),
        filters=filters,
        # The query is embedded with the slot in force, which is also the model the *next*
        # ingest will write with — so after a flip this arm searches a corpus that is partly
        # the other vector space until `tools.reembed run` drains it. Sanctioned by R-87(1)
        # and made finite by the staleness report; both arms still fuse under R-37's RRF, and
        # a 3072-dim vector from either model is a legal operand for `dense_distance`.
        sessionmaker=ctx.sessionmaker,
        embeddings=embeddings,
        embedding_model=_model_for(ctx, ModelSlot.EMBEDDING),
        retriever_factory=retriever_factory,
        prefetch=ctx.prefetch,
    )
    return {"retrieved_chunk_ids": [str(hit.chunk_id) for hit in hits]}


async def rerank(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-02 — rerank the merged candidates to the grounding top-K (T-306, R-47).

    `app.rag.rerank` owns the rubric, the prompt and the scoring; this node owns the two
    reads around it.

    **Two failure modes, deliberately different.** The database read has no `try` and fails
    **closed**, exactly as `retrieve` does: without the chunk text there is nothing to ground
    in, and FR-RET-05 says the system returns a graceful error rather than fabricating —
    `handle_node_error` files it as `RETRIEVAL_UNAVAILABLE`. The *model* call fails **open**
    inside `rerank_passages` (R-47(2)), because the candidates arrive already ordered by the
    R-46(3) merge and truncating that order is a worse answer than no answer only if you
    believe the reranker is load-bearing for correctness, which it is not.

    The chunk text is re-read rather than carried: `RAGState` holds ids and scalars only
    (R-42(2), FR-PER-03). It is read back through `_retrieval_filter` — the *same* scope
    `retrieve` used, not a reconstruction of it.
    """
    from app.db.repositories.retrieval import fetch_chunks
    from app.rag.rerank import prompt_sources, rerank_passages

    ctx = runtime.context
    settings = get_settings()
    retrieved = [str(chunk_id) for chunk_id in state.get("retrieved_chunk_ids") or []]
    if not retrieved:
        # R-23: nothing retrieved is a real state of the world, and `generate` abstains on it.
        return {"reranked_chunk_ids": [], "rerank_scores": []}

    filters = _retrieval_filter(state, ctx)
    if filters is None:  # pragma: no cover - `retrieve` already returned [] on this branch
        return {"reranked_chunk_ids": [], "rerank_scores": []}

    chunk_ids = _parse_chunk_ids(retrieved)

    async with ctx.sessionmaker() as session:
        hits = await fetch_chunks(session, chunk_ids, filters=filters)

    if not hits:
        # Everything retrieved has since been deleted or fallen out of scope. FR-CIT-06(1)/(5)
        # would have dropped these citations at serve time anyway; abstaining is the same
        # answer, one stage earlier and without a wasted generation.
        log.warning(
            "graph.rerank_candidates_vanished",
            conversation_id=str(ctx.conversation_id),
            retrieved=len(chunk_ids),
        )
        return {"reranked_chunk_ids": [], "rerank_scores": []}

    if not settings.graph.rerank_enabled:
        # A cost switch, not the failure path (R-47(2) already fails open) — hence `info`,
        # like the router's. Off, grounding is the top-K of the merged order and no
        # FR-CIT-04 score is published, which is the same contract the fallback path has.
        log.info("rag.rerank.disabled", conversation_id=str(ctx.conversation_id))
        top = [str(hit.chunk_id) for hit in hits][: settings.rerank.top_k]
        return {"reranked_chunk_ids": top, "rerank_scores": []}

    chat = ctx.chat
    if chat is None:
        from app.services.llm import get_chat_client

        chat = get_chat_client()

    outcome = await rerank_passages(
        query=state.get("query", ""),
        sources=prompt_sources(hits),
        chat=chat,
        settings=settings,
        model=_model_for(ctx, ModelSlot.RERANK),
    )
    return {
        "reranked_chunk_ids": list(outcome.chunk_ids),
        # Empty unless every passage in the top-K carries a real score (R-47(2)). The
        # cross-probe RRF score is **not** substituted — R-46(3) makes it incomparable across
        # turns, and FR-CIT-04 shows this number to a user.
        "rerank_scores": list(outcome.scores),
    }


def _abstain_empty_scope() -> RAGState:
    """R-23 / FR-SYS-02 / §4.12 precedence note (3) — nothing to ground in, so say so.

    Both of `generate`'s empty branches answer identically, and deliberately: "retrieval found
    nothing" and "everything retrieval found has since been deleted" are the same fact from the
    user's seat, and giving the second its own copy would explain an internal race to someone
    who cannot act on it.
    """
    return {
        "answer": ABSTAIN_EMPTY_SCOPE,
        "outcome": "abstained",
        "citation_ids": [],
        "gate_verdict": "pass",
    }


def _realign_scores(
    previous_ids: Sequence[str], surviving_ids: Sequence[str], scores: Sequence[float]
) -> list[float]:
    """Keep `rerank_scores` positionally aligned when a chunk disappears (R-47(2), R-48(5)).

    `rerank_scores` is either empty or exactly as long as `reranked_chunk_ids` — there is no
    partial state, because the channel holds position-aligned floats and FR-CIT-04 shows those
    numbers to a user. So narrowing the id list has to carry the scores across by **id**, and
    anything that does not add up returns empty rather than guessing: an unscored citation
    renders with no number, which R-47(2) already requires clients to handle.
    """
    if len(scores) != len(previous_ids):
        return []
    by_id = dict(zip(previous_ids, scores, strict=True))
    if any(chunk_id not in by_id for chunk_id in surviving_ids):  # pragma: no cover - a subset
        return []
    return [by_id[chunk_id] for chunk_id in surviving_ids]


async def generate(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 5 / FR-SYS-02 — grounded generation (T-307, R-48).

    `app.rag.generation` owns the prompt call and the `[S<n>]` segmentation; this node owns
    the two reads around it and the state it writes.

    **Fails closed** — the third node to do so, with `screen` and `retrieve`, and the
    deliberate inverse of `route` (R-45(2)) and `rerank` (R-47(2)). Those two have a
    defensible degraded output; this one's would be an answer that is not grounded, which
    R-23, FR-SYS-02 and FR-RET-05 forbid in the same breath. There is no `try` and no
    `GRAPH_GENERATE_ENABLED`: a cost switch needs an off state, and this node's is "no
    product".

    The chunk text is re-read rather than carried (`RAGState` holds ids and scalars only,
    R-42(2)) and re-read through `_retrieval_filter` — the *same* scope `retrieve` built, for
    the reason R-47(5) records: a second construction of the FR-ORC-06 scope is how the
    R-46(2) hole reopens one node later.

    **The `[S<k>]` → `reranked_chunk_ids[k-1]` invariant is established here** (R-48(5)). The
    sources are composed in the order state lists them, so a marker resolves from checkpointed
    state alone — which is what makes R-44(7)'s binding on T-308 implementable across a node
    boundary, where a `ComposedPrompt` cannot travel. When a chunk has vanished since `rerank`
    ran, the id list is **narrowed** and the scores realigned in step, rather than leaving
    state describing a context the model was never given.
    """
    from app.db.repositories.retrieval import fetch_chunks
    from app.rag.generation import generate_answer
    from app.rag.history import load_history, to_messages
    from app.rag.rerank import prompt_sources

    reranked = [str(chunk_id) for chunk_id in state.get("reranked_chunk_ids") or []]
    if not reranked:
        return _abstain_empty_scope()

    ctx = runtime.context
    filters = _retrieval_filter(state, ctx)
    if filters is None:  # pragma: no cover - `retrieve` already returned [] on this branch
        return _abstain_empty_scope()

    async with ctx.sessionmaker() as session:
        hits = await fetch_chunks(session, _parse_chunk_ids(reranked), filters=filters)
        if not hits:
            log.warning(
                "graph.generate_candidates_vanished",
                conversation_id=str(ctx.conversation_id),
                grounding=len(reranked),
            )
            return _abstain_empty_scope()
        # R-30 sends the **whole** transcript — FR-STA-04's warn-and-block is what keeps it in
        # range, so there is no windowing to do — but stops short of the turn being answered
        # (R-48(7)): that row is already in `messages`, and `compose_messages` appends the
        # query itself.
        history = to_messages(
            await load_history(
                session,
                ctx.conversation_id,
                until_message_id=_message_uuid(state.get("user_message_id")),
            )
        )

    update: RAGState = {}
    surviving = [str(hit.chunk_id) for hit in hits]
    if surviving != reranked:
        log.warning(
            "graph.grounding_narrowed",
            conversation_id=str(ctx.conversation_id),
            before=len(reranked),
            after=len(surviving),
        )
        update["reranked_chunk_ids"] = surviving
        update["rerank_scores"] = _realign_scores(
            reranked, surviving, state.get("rerank_scores") or []
        )

    chat = ctx.chat
    if chat is None:
        from app.services.llm import get_chat_client

        chat = get_chat_client()

    generated = await generate_answer(
        query=state.get("query", ""),
        # The same mapping the reranker used, deliberately shared (R-47(1) note): two
        # independent chunk→source mappings is how the filename in a citation stops matching
        # the filename the model was told.
        sources=prompt_sources(hits),
        history=history,
        chat=chat,
        model=_model_for(ctx, ModelSlot.CHAT),
    )
    if generated.source_ids != tuple(surviving):  # pragma: no cover - invariant guard
        # Unreachable while the sources are composed from `hits` in order. It is checked
        # anyway because the downstream failure is silent: T-308 would validate citations
        # against a differently-ordered set and **pass** (R-44(7)).
        raise RuntimeError("prompt source order does not match the grounding set")

    update["answer"] = generated.text
    update["model_name"] = generated.model
    update["prompt_tokens"] = generated.prompt_tokens
    update["completion_tokens"] = generated.completion_tokens
    return update


async def gate(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-07 / FR-RET-05 / FR-CIT-06 — groundedness + citation verification (T-308, R-49).

    NFR-PRF-02 is why this is a node and not a filter on the stream: the client sees
    nothing until this passes. **The (D) that offered incremental verification as the escape
    is declined** — with R-49 the gate costs sub-millisecond, so the ≈5–6 s wait is entirely
    upstream and trading FR-CIT-06's "before an answer is served" away would not recover it.

    **Four of the five checks are already discharged** (R-48, T-308 board line (b)):
    `generate`'s fail-closed `fetch_chunks` read-back went through the *same* `RetrievalFilter`
    `retrieve` built, so (1) exists, (2) was in the context passed, (3) authorized and (5)
    still ACTIVE hold for the whole grounding set, and an out-of-range marker was already
    dropped. Re-running them here would be a second construction of the FR-ORC-06 scope, which
    is the R-46(2) hole one node later (R-47(5)) — so **this node does no database work at
    all**, which `tests/test_graph.py` asserts rather than merely documents.

    **Fails closed**, the fourth node to do so with `screen`, `retrieve` and `generate`, and
    the inverse of `route` (R-45(2)) and `rerank` (R-47(2)). Those two degrade to a real
    fallback; this one's degraded output would be "serve an answer whose grounding was not
    checked", which is not a degradation of FR-CIT-06 but its absence. There is no `try` and
    no `GATE_ENABLED`. It costs nothing in availability, because with no I/O and no model call
    the only way this node can raise is a bug.
    """
    from app.rag.generation import split_answer_segments
    from app.rag.groundedness import (
        GATE_ABSTAINED,
        GATE_COMPLETED,
        GATE_RETRY,
        assess,
        hypothetical_probe,
    )

    if state.get("outcome") == "abstained":
        # The R-23 empty-scope branch already decided, and it is not a groundedness failure:
        # writing a score here would put a 0.0 on a turn that never generated anything.
        return {"gate_verdict": "pass"}

    settings = get_settings()
    # **From state, not from `rerank`'s update** (board line (c)): `generate` narrows this list
    # when a chunk vanished between supersteps, and the marker map follows the narrowed list.
    source_ids = [str(chunk_id) for chunk_id in state.get("reranked_chunk_ids") or []]
    answer = state.get("answer")
    if not answer:  # pragma: no cover - `generate` fails closed on an empty answer
        # Abstaining *is* the closed direction here. Raising would file an impossible state as
        # `SYSTEM_FAILURE`, which teaches an operator nothing they can act on.
        return {"gate_verdict": "abstain", "gate_reason": "no_answer", "groundedness": 0.0}

    started = time.perf_counter()
    # The **one** parser (R-48(4)). Re-deriving the marker map here is what R-44(7) forbids:
    # a mapping rebuilt from a differently ordered collection validates against the wrong set
    # and *passes*.
    segments, dropped = split_answer_segments(answer, source_ids)
    report = assess(segments, markers_dropped=dropped, settings=settings)

    # FR-CIT-06 (1)/(2)/(3)/(5) in one line, and it is a tautology by construction rather than
    # a re-check: every id here came out of `source_ids`, which `generate` proved equal to the
    # sources it composed. It is asserted anyway because the failure it guards is silent.
    if not set(report.citation_ids) <= set(source_ids):  # pragma: no cover - invariant guard
        raise RuntimeError("citation resolved outside the grounding set")

    probe = hypothetical_probe(segments, settings=settings)
    verdict: GateVerdict
    reason: str | None
    if report.score >= settings.gate.min_groundedness:
        verdict, reason = "pass", None
    elif not report.citations and probe is not None:
        # The only case worth ~4.6 s: retrieval clearly missed and the model had nothing to
        # cite, which is also the only case where a HyDE probe has a real chance. A partially
        # supported answer abstains immediately rather than paying to find the same passages.
        verdict, reason = "retry", "no_citations"
    else:
        verdict, reason = "abstain", report.reason or "low_coverage"

    event = {"pass": GATE_COMPLETED, "retry": GATE_RETRY}.get(verdict, GATE_ABSTAINED)
    log.info(
        event,
        conversation_id=str(runtime.context.conversation_id),
        turn_index=state.get("turn_index"),
        verdict=verdict,
        reason=reason,
        groundedness=round(report.score, 4),
        claims=report.claims,
        supported=report.supported,
        citations=report.citations,
        sources=len(source_ids),
        markers_dropped=report.markers_dropped,
        retry_count=state.get("retry_count", 0),
        elapsed_us=int((time.perf_counter() - started) * 1_000_000),
    )
    return {
        "groundedness": report.score,
        "gate_verdict": verdict,
        "gate_reason": reason,
        "citation_ids": list(report.citation_ids),
    }


async def adapt(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-RET-05 — modify the strategy and retry (T-308, R-49).

    **The only writer of `retry_count` and the only way back into `retrieve`.** Both
    halves matter: a second writer breaks the FR-ORC-07 bound, and a second back-edge
    would let a cycle run without incrementing it.

    **The modification is HyDE from the rejected answer.** FR-RET-05 says "retries with a
    modified retrieval strategy" and defines nothing further, and the code narrows the options
    much further than that sentence suggests: `route` does **not** re-run on this back edge,
    and `retrieve_for_turn` reads only `query` and `sub_queries` — so writing a new `strategy`
    on its own would be a no-op dressed as a modification. The two obvious levers are also
    closed: `RETRIEVAL_MERGED_TOP_K`/`RERANK_TOP_K` are settings rather than state, so
    "widen the search" is not expressible without a `RAGState` field, and clearing
    `mentioned_document_ids` is the one narrowing R-46(1) exists to protect.

    What is left is free and textbook: the rejected answer **is** a hypothetical document.
    Embedding an *answer* retrieves different neighbours than embedding a *question*, so the
    merged set genuinely differs and the retry is not a re-roll of the same dice — and R-45(3)
    already sanctioned exactly this shape ("HyDE's passage is an ordinary probe"). It costs
    zero model calls. `strategy` is written for the record even though retrieval does not read
    it, because `query_class` and `strategy` are what a telemetry reader has to reconstruct
    the turn from.

    Single-shot by construction, which is why `Settings` refuses `GRAPH_MAX_RETRIES > 1`: a
    second cycle would compose the *second* rejected answer on top of the first.
    """
    from app.rag.generation import split_answer_segments
    from app.rag.groundedness import GATE_ADAPTED, hypothetical_probe

    update: RAGState = {"retry_count": state.get("retry_count", 0) + 1}

    answer = state.get("answer")
    if not answer:  # pragma: no cover - `gate` only emits `retry` with an answer in hand
        return update
    source_ids = [str(chunk_id) for chunk_id in state.get("reranked_chunk_ids") or []]
    segments, _ = split_answer_segments(answer, source_ids)
    probe = hypothetical_probe(segments)
    if probe is None:  # pragma: no cover - `gate` checks the same floor before emitting retry
        return update

    # Appended, never substituted: R-45(3) makes probes additive and `build_probes` always
    # searches the original query too, so a useless probe costs recall in the merge and can
    # never remove the original signal — which is the whole reason this is safe to do without
    # being able to tell a fabrication from a decline.
    update["sub_queries"] = [*(state.get("sub_queries") or []), probe]
    update["strategy"] = "hyde"
    log.info(
        GATE_ADAPTED,
        conversation_id=str(runtime.context.conversation_id),
        turn_index=state.get("turn_index"),
        retry_count=update["retry_count"],
        probes=len(update["sub_queries"]),
        probe_chars=len(probe),
    )
    return update


async def abstain(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-RET-05 / R-23 — decline to answer.

    A terminal *node*, not an edge to `END`: an abstention is a response. It is persisted,
    telemetered and rendered like any other, so it must pass through `finalize`.

    The blocked branch answers `BLOCKED_INJECTION`, not `SYSTEM_FAILURE`. Until T-303 it did the
    latter, which FR-ERR-04 reserves for *unclassified errors* — a blocked prompt is neither
    unclassified nor an error, and R-43(6) had already said injection-`blocked` carries its own
    copy. No `error_code` is set on any branch (R-44(5)).

    **The gate branch replaces the answer rather than passing it through, and that is a defect
    T-308 had to fix.** This node used to answer `state["answer"] or ABSTAIN_EMPTY_SCOPE` on
    every non-blocked path, which was harmless while the only abstention was the empty-scope
    one — where `answer` is already our own copy. Once the gate can reject a *generated*
    answer, the same line would serve the rejected text back under an `abstained` outcome:
    strictly worse than serving it as an answer, because the user would read ungrounded prose
    carrying no citations *and* an honest-looking label. The cost is real and accepted — when
    the model declined in its own words that phrasing is lost — because telling a decline from
    a fabrication needs exactly the semantic read R-49 declines to make before serving.
    """
    if state.get("injection_verdict") == "blocked":
        return {"outcome": "blocked", "answer": BLOCKED_INJECTION, "citation_ids": []}
    if state.get("gate_verdict") in ("retry", "abstain"):
        return {
            "outcome": "abstained",
            "answer": ABSTAIN_LOW_GROUNDEDNESS,
            "citation_ids": [],
        }
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


def _should_persist(state: RAGState) -> bool:
    """Does this turn become a `messages` row? (T-402)

    `answered` and `abstained` do: an abstention is a response (R-23), it is what the user
    read, and FR-MSG-08 Regenerate needs a row to replace.

    `error` does **not**. The FR-ERR-04 copy goes to the client over the stream and stops
    there, so a provider blip does not leave `"System Failure: Please try again."` in a
    durable transcript, counted by the FR-ANL cards and — the sharper cost — charged against
    the NFR-CAP-01 budget that R-51(4) derives from `messages`. R-43(5) already records that
    an errored turn is logs-only until T-604 supplies durable records; this keeps that true.

    `blocked` splits, because two unrelated events share the outcome. An injection-blocked
    turn is a response to a question the user actually asked and belongs in their transcript
    (R-44(5) gives it its own copy for exactly that reason). An FR-ORC-02 access denial is
    not their conversation at all — and on the branch that produces it the conversation may
    not even exist any more (`govern`'s `no_such_conversation`), so the insert would fail on
    its own foreign key.
    """
    outcome = state.get("outcome")
    if outcome in ("answered", "abstained"):
        return True
    if outcome == "blocked":
        return state.get("injection_verdict") == "blocked"
    return False


async def _persist_turn(
    state: RAGState,
    ctx: RAGContext,
    *,
    latency_ms: int | None,
    replace_id: str | None = None,
) -> str | None:
    """Write the AI `messages` row for this turn and return its id (T-402, §4.16, FR-MSG-06).

    With ``replace_id`` this **updates that row in place** instead of inserting — FR-MSG-08's
    Regenerate (T-404). Replace rather than append, because §4.16 makes the transcript what the
    user sees and the FR-ANL cards count: an appended answer would show the same question
    answered twice, and NFR-CAP-01's budget (derived from `messages`, R-51(4)) would charge the
    conversation for both.

    **The chunk read here is load-bearing and R-49(3) leans on it.** The gate does no database
    work at all, on the argument that this read runs a superstep later and re-checks
    FR-CIT-06 (1)/(3)/(5) for free: a chunk deleted or revoked between the gate and now is
    absent from `hits` and its citation is dropped by :func:`app.rag.citations.build_citations`
    rather than rendered as a chip addressing nothing. Weakening or removing it reopens those
    checks at serve time.

    It goes through `_retrieval_filter` — the *same* scope helper `retrieve`, `rerank` and
    `generate` use — for the reason R-47(5) records: a second construction of the FR-ORC-06
    scope is how the R-46(2) hole reopens one node later.

    `split_answer_segments` is **the one parser** (R-48(4)). It is not re-derived here and
    must never be: a marker map rebuilt from a differently ordered collection validates
    against the wrong set and passes (R-44(7)).

    Three things this deliberately does **not** write:

    * **`evaluation`** — that column is DeepEval's alone (R-50). It stays NULL until
      `workers/evaluate.py` fills it, and `RAGState.groundedness` never goes near it: the
      gate's structural number and Faithfulness measure the same property by different means
      and must not be conflated in front of one user (R-49(1), OI-34 as resolved).
    * **a substituted FR-CIT-04 score** — absent when the reranker failed open (R-47(2)).
    * **`feedback`** — FR-MSG-08's column, written by T-403. **On the replace path it is
      cleared**, which is not a contradiction: R-55(5) constrains what the *feedback route*
      may touch, not what may touch feedback. A 👎 followed by Regenerate is the common
      sequence, so carrying the thumb forward would attach a negative rating to a new and
      possibly good answer with nothing in the row recording that the text changed — and
      the R-80 calibration report consumes `(answer, thumb)` pairs, where a mismatched pair
      is worse than a missing one, since it would count a thumb cast on text the user never
      saw. `evaluation` is cleared on exactly this reasoning (R-50(5));
      clearing one and keeping the other leaves the row half-stale undetectably.
    """
    from sqlalchemy import update

    from app.db.enums import MessageRole
    from app.db.models.message import Message
    from app.db.repositories.retrieval import fetch_chunks
    from app.rag.citations import build_citations
    from app.rag.generation import cited_chunk_ids, split_answer_segments

    answer = state.get("answer")
    if not answer:  # pragma: no cover - every persisted outcome sets one
        return None

    # Only a model-authored answer indexes into the grounding set. An abstention or a blocked
    # turn carries *our* copy, which no `[S<n>]` marker addresses — passing the grounding set
    # there would invite a stray marker in a copy string to resolve to a real passage.
    grounded = state.get("outcome") == "answered"
    source_ids = [str(cid) for cid in state.get("reranked_chunk_ids") or []] if grounded else []

    segments, _ = split_answer_segments(answer, source_ids)
    cited = _parse_chunk_ids(cited_chunk_ids(segments))

    async with ctx.sessionmaker() as session:
        hits = []
        if cited:
            filters = _retrieval_filter(state, ctx)
            if filters is not None:
                hits = await fetch_chunks(session, cited, filters=filters)

        envelope, dropped = build_citations(
            segments=segments,
            source_ids=source_ids,
            hits=hits,
            scores=state.get("rerank_scores") or [],
        )
        if dropped:
            log.warning(
                "graph.citations_dropped",
                conversation_id=str(ctx.conversation_id),
                dropped=dropped,
                cited=len(cited),
            )

        if replace_id is not None:
            # **`content` and `citations` are written together, always.** T-309 replays
            # `split_answer_segments` against `citations.source_ids`, so a row carrying new
            # content beside the old envelope validates against the wrong grounding set *and
            # passes* — a silent, permanent mis-scoring.
            #
            # `evaluation` and `feedback` clear in this **same statement** rather than at the
            # route. A route-level pre-clear that is then followed by a failed re-run destroys
            # the scores and the rating of an answer that still exists; here the row is only
            # ever touched by a turn that produced a replacement.
            #
            # `id`, `seq`, `conversation_id`, `role` and `created_at` are untouched: `seq`
            # carries display order (T-108) and `messages` has no `updated_at`, so moving
            # `created_at` would misreport when the exchange happened.
            updated = await session.execute(
                update(Message)
                .where(Message.id == uuid.UUID(replace_id))
                .values(
                    content=answer,
                    citations=envelope,
                    model_name=state.get("model_name"),
                    prompt_tokens=state.get("prompt_tokens"),
                    completion_tokens=state.get("completion_tokens"),
                    latency_ms=latency_ms,
                    evaluation=None,
                    feedback=None,
                )
            )
            await session.commit()
            if not updated.rowcount:
                # The row was deleted mid-turn (the conversation went away). Nothing to serve
                # and nothing to report: returning `None` leaves `answer` in state, which is
                # the same degradation an INSERT failure produces.
                log.warning(
                    "graph.replace_target_missing",
                    conversation_id=str(ctx.conversation_id),
                    message_id=replace_id,
                )
                return None
            return replace_id

        # Minted here rather than left to the column default, which SQLAlchemy applies at
        # flush: the id is needed *after* the commit and reading it back off the instance
        # would make this depend on flush semantics. `str(None)` is a truthy `"None"`, so
        # getting that wrong sets `answer_message_id` to a string that looks persisted and is
        # not — the T-201 lesson (`documents` mints its own id for the same reason).
        message_id = uuid.uuid4()
        message = Message(
            id=message_id,
            conversation_id=ctx.conversation_id,
            role=MessageRole.AI,
            content=answer,
            citations=envelope,
            model_name=state.get("model_name"),
            prompt_tokens=state.get("prompt_tokens"),
            completion_tokens=state.get("completion_tokens"),
            latency_ms=latency_ms,
        )
        session.add(message)
        await session.commit()

    return str(message_id)


async def finalize(state: RAGState, runtime: Runtime[RAGContext]) -> RAGState:
    """FR-ORC-01 step 6 — the `finally` block: unlock, close telemetry, serve.

    Reached on **every** path, which is what §4.12 precedence note (2) asks for. It releases
    the R-24 lock, writes the telemetry end, and persists the turn's `messages` row
    (:func:`_persist_turn`) — in that last order deliberately: the row is written *before*
    the lock is freed, so there is no window in which the GUI is told the turn is over and
    the answer it should render does not exist yet.

    **Three side effects, three different failure directions**, and none of them may take the
    turn down (see below).

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

    started_at = state.get("started_at")
    latency_ms = int((time.time() - started_at) * 1000) if started_at is not None else None

    # --- persist the turn (T-402) --------------------------------------------
    #
    # Idempotent on resume by construction: `answer_message_id` is checked *before* the
    # insert, because the app writes through asyncpg and the checkpointer through psycopg
    # and the two can never share a transaction (R-42, T-302's note). A run that committed
    # this row and then died before its checkpoint landed resumes here and writes nothing.
    #
    # Guarded, because **this node must never raise**: its own error handler routes back to
    # `finalize`, so an exception here would loop until the recursion limit instead of
    # failing once. On failure `answer_message_id` stays unset, which leaves `answer` in
    # state as the run's only copy — the caller still serves it, and nothing is lost that was
    # not already lost.
    # **The settled state, not `state`.** `outcome` is written by *this node's* update on the
    # happy path — the gate routes a passing turn straight here without setting one — so
    # reading `state` alone sees `None` and treats a successfully answered turn as ungrounded.
    # That is not a cosmetic difference: `_persist_turn` derives the grounding set from the
    # outcome, so it would persist every real answer with an empty `source_ids` and drop every
    # citation on the floor. Caught live, by a turn whose model cited correctly and whose row
    # came back with no chips.
    # **Two fields, two meanings, deliberately not merged** (T-404). `answer_message_id` means
    # "this turn's row is already written"; `regenerate_message_id` means "write into that row
    # instead of inserting". Resume then works in both directions from one guard, because
    # `answer_message_id` is set only after the commit: a resumed send skips the INSERT, and a
    # resumed regenerate redoes the UPDATE — idempotent by shape, where a redone INSERT is not.
    settled: RAGState = {**state, **update}
    answer_message_id = state.get("answer_message_id")
    replace_id = state.get("regenerate_message_id")
    if not answer_message_id and _should_persist(settled):
        try:
            answer_message_id = await _persist_turn(
                settled, ctx, latency_ms=latency_ms, replace_id=replace_id
            )
        except Exception:
            log.exception(
                "graph.persist_failed",
                conversation_id=str(ctx.conversation_id),
                turn_index=state.get("turn_index"),
            )
    if answer_message_id:
        update["answer_message_id"] = answer_message_id
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

    if latency_ms is not None:
        # The **same** number `_persist_turn` wrote to `messages.latency_ms`, not a second
        # reading taken after it. R-43(5) makes those metric columns the FR-ORC-03 telemetry
        # record for MVP, so NFR-OBS-02's "the stats panel matches telemetry" holds by
        # identity — which it would stop doing the moment the two clocks were read apart.
        #
        # **One record, three sinks** (T-604, R-79(1)). It is built once and handed to the
        # log event, the durable `turn_telemetry` row and the optional OTel span, so the
        # identity above extends to all three by construction rather than by three call
        # sites assembling matching argument lists. `TurnRecord` is ids and scalars only, so
        # none of the three can be handed payload text (R-43(5) made unrepresentable).
        record = telemetry.TurnRecord(
            conversation_id=ctx.conversation_id,
            owner_id=ctx.owner_id,
            turn_index=state.get("turn_index"),
            outcome=update.get("outcome") or state.get("outcome"),
            latency_ms=latency_ms,
            error_code=state.get("error_code"),
            model_name=state.get("model_name"),
            prompt_tokens=state.get("prompt_tokens"),
            completion_tokens=state.get("completion_tokens"),
            message_id=_as_uuid(answer_message_id),
            started_at=started_at,
            # T-609/R-80(1): the gate's own score, durable in the operator store only.
            # `RAGState.groundedness` is otherwise transient — it is deliberately never
            # written to `messages.evaluation` (R-49(1)), so without this the threshold that
            # decides whether an answer is served has no retained input to calibrate against.
            groundedness=state.get("groundedness"),
        )
        telemetry.turn_closed(record)
        record_turn_span(record)
        await _record_turn_telemetry(ctx, record)
    return update


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """`answer_message_id` is a *string* in `RAGState` (R-42(2): ids and scalars).

    Unparseable is `None` rather than an exception: this runs inside `finalize`, and a
    malformed id is worth losing the join for, never the whole telemetry row.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except ValueError, AttributeError, TypeError:
        return None


async def _record_turn_telemetry(ctx: RAGContext, record: telemetry.TurnRecord) -> None:
    """Write the durable NFR-OBS-01 row (T-604, R-79(1)).

    **Its own session and its own transaction**, not the persist step's. Sharing one would
    let a telemetry failure roll back the answer a user is already being shown — the tail
    wagging the dog — and the identity NFR-OBS-02 asks for is between the *values*, which
    come from one `TurnRecord`, not between the transactions.

    **Guarded, and last.** `finalize` must never raise (its own error handler routes back
    here, so an exception would loop to the recursion limit), and this is the one of its
    side effects nobody is waiting on: it runs after the row the GUI renders and after the
    R-24 lock is freed, so a slow or failing telemetry write cannot delay either. A lost
    row costs an operator one record; the `graph.turn.*` event still fired.
    """
    try:
        async with ctx.sessionmaker() as session:
            await TurnTelemetryRepository(session).record(record)
            await session.commit()
    except Exception:
        log.warning(
            "telemetry.record_failed",
            conversation_id=str(ctx.conversation_id),
            turn_index=record.turn_index,
            exc_info=True,
        )


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
