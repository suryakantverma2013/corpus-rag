"""Turn-level retrieval: probe fan-out and the cross-probe merge (T-305, R-46, FR-ORC-06).

`HybridRetriever` (T-206) answers *one* query. R-45(3) made a turn ask up to four —
``[query] + sub_queries``, where the derived probes are a refinement, a decomposition, or a
HyDE passage and the original query is **always** one of them. Turning that into a single
ordered candidate list is this module's whole job, and it is deliberately kept out of
`app.rag.graph`: like `app.rag.router` and `app.rag.errors` it imports **no langgraph**, so
it is testable (and reusable) without pulling in the checkpointer's serializer flag.

Three things here are rulings rather than implementation taste:

* **Probes are additive and the original query is never dropped** (R-45(3)). An empty
  ``sub_queries`` therefore means "just the query", and a bad router rewrite can lose
  recall but can never remove the original signal.
* **The merge is a second RRF pass** (R-46(3)) — see :func:`app.rag.fusion.rrf_merge`.
* **A derived probe may fail; the original query may not** (R-46(6)). Losing a probe costs
  recall, which is what "additive" means. Losing the query means there is no grounding, and
  FR-RET-05 is explicit that the system then returns a graceful error rather than
  fabricating — so that exception propagates and `retrieve` fails closed.

The fan-out is `asyncio.gather` over one **session per probe**. Not a micro-optimisation:
`AsyncSession` is not safe for concurrent statements, which is exactly why `HybridRetriever`
runs its own two arms sequentially — sharing one session across gathered probes is the same
bug with a longer fuse.

T-311 adds one wrinkle to that first bullet. Because the original query is additive to
nothing, its arm does not have to wait for the router: `route` may run it early through
:func:`prefetch_query_arm` and leave the hits in an `app.rag.prefetch.QueryArmPrefetch`,
which :func:`retrieve_for_turn` then splices back in at index 0. Everything below is written
so that path is a pure latency change — the same probes, in the same order, merged the same
way, failing in the same direction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

import structlog

from app.config import Settings, get_settings
from app.rag.fusion import drop_overlapping_neighbours, rrf_merge
from app.rag.prefetch import QueryArmPrefetch
from app.rag.retrieval import RetrievalFilter, RetrievedChunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.rag.retrieval import Retriever
    from app.services.embeddings import EmbeddingClient

log = structlog.get_logger(__name__)

__all__ = ["build_probes", "prefetch_query_arm", "retrieve_for_turn"]

#: Telemetry names. `rag.*` rather than `graph.turn.*` on the R-45(8) precedent: the closed
#: `graph.turn.*` vocabulary is a span-pairing contract (R-43(5)) and this is neither end of
#: a span. Counts and codes only — **never** the query, a probe, or chunk text.
RETRIEVAL_COMPLETED = "rag.retrieval.completed"
PROBE_FAILED = "rag.retrieval.probe_failed"
#: T-311. `.prefetched` carries the arm's own elapsed time, which is the only place the
#: overlap is observable: `.completed`'s timing includes a wait that may already be over.
QUERY_ARM_PREFETCHED = "rag.retrieval.prefetched"
QUERY_ARM_FAILED = "rag.retrieval.prefetch_failed"


def build_probes(
    query: str,
    sub_queries: Iterable[str],
    *,
    settings: Settings | None = None,
) -> list[str]:
    """``[query] + sub_queries``, cleaned and bounded. The query is always index 0.

    Everything here is defence against state rather than against the router, which already
    applies the same caps: ``sub_queries`` arrives from a **checkpoint**, which may have
    been written by an older deployment under a larger ``ROUTER_MAX_SUB_QUERIES`` or a
    larger ``ROUTER_MAX_PROBE_CHARS``. A resumed turn must cost what this deployment's
    settings say it costs, so the caps are re-applied on read.

    Truncation, never rejection — R-45(3)'s rule, for its reason: silently discarding an
    over-long HyDE passage would downgrade the strategy to plain hybrid with nothing in the
    logs to say so.
    """
    settings = settings or get_settings()
    router = settings.router

    probes = [query.strip()]
    for candidate in sub_queries:
        if len(probes) > router.max_sub_queries:
            break
        if not isinstance(candidate, str):
            continue
        probe = candidate.strip()[: router.max_probe_chars].strip()
        # A probe equal to the query is not a second opinion, it is a second bill: it
        # returns the identical list and doubles that list's weight in the merge.
        if not probe or probe in probes:
            continue
        probes.append(probe)
    return probes


async def _search_one(
    probe: str,
    vector: Sequence[float],
    *,
    filters: RetrievalFilter,
    sessionmaker: async_sessionmaker[AsyncSession],
    retriever_factory: Callable[[AsyncSession], Retriever],
    top_k: int,
) -> list[RetrievedChunk]:
    """One probe, one session. The session closes before the merge touches the results."""
    async with sessionmaker() as session:
        return await retriever_factory(session).search(probe, vector, filters=filters, top_k=top_k)


async def prefetch_query_arm(
    slot: QueryArmPrefetch,
    *,
    query: str,
    filters: RetrievalFilter,
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingClient,
    embedding_model: str | None = None,
    retriever_factory: Callable[[AsyncSession], Retriever],
    settings: Settings | None = None,
) -> None:
    """Search the original query's arm early, for `retrieve` to collect (T-311).

    The whole optimisation in one function: this is exactly the work
    :func:`retrieve_for_turn` does for ``probes[0]``, run before the router's answer exists
    because R-45(3) means it never depended on it. Same probe normalisation, same
    `fusion_top_k`, same one-session rule — a prefetched arm and a searched arm must be
    indistinguishable in the merge, or this stops being a latency change.

    **Never raises.** It runs inside `route`, which fails *open* (R-45(2)), and a routing
    node must not be able to fail a turn. But the failure is *carried*, not swallowed: it
    goes into the slot and :func:`retrieve_for_turn` re-raises it, so the turn still fails
    closed at the node that owns that direction (R-46(6)). `BaseException` is deliberately
    not caught — a cancellation is not a retrieval failure to report later.
    """
    settings = settings or get_settings()
    probe = build_probes(query, (), settings=settings)[0]
    if not probe:
        # Nothing to search and nothing to record. `retrieve_for_turn` short-circuits the
        # same input to `[]`, and R-23 abstains downstream.
        return

    started = time.perf_counter()
    try:
        vectors = await embeddings.embed_queries([probe], model=embedding_model)
        hits = await _search_one(
            probe,
            vectors[0],
            filters=filters,
            sessionmaker=sessionmaker,
            retriever_factory=retriever_factory,
            top_k=settings.retrieval.fusion_top_k,
        )
    except Exception as exc:
        slot.publish(query=probe, filters=filters, error=exc)
        log.warning(
            QUERY_ARM_FAILED,
            error=type(exc).__name__,
            error_code=getattr(exc, "code", None),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return

    slot.publish(query=probe, filters=filters, hits=hits)
    log.info(
        QUERY_ARM_PREFETCHED,
        candidates=len(hits),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def retrieve_for_turn(
    *,
    query: str,
    sub_queries: Iterable[str] = (),
    filters: RetrievalFilter,
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingClient,
    embedding_model: str | None = None,
    retriever_factory: Callable[[AsyncSession], Retriever],
    prefetch: QueryArmPrefetch | None = None,
    settings: Settings | None = None,
) -> list[RetrievedChunk]:
    """Retrieve for one turn: fan the probes out, merge, dedupe, truncate.

    Raises whatever the embedding call or the **original query's** search raises — the node
    above has no `try`, and FR-RET-05 wants a graceful failure here rather than an answer
    grounded in nothing (R-46(6)). A query arm that `route` prefetched (T-311) raises here
    too, at the node whose failure direction is the right one.

    ``prefetch`` is consumed if — and only if — it was filled for *this* probe text under
    *this* scope; anything else and the query is searched here as it always was. Everything
    downstream of the fan-out is common to both paths on purpose.
    """
    settings = settings or get_settings()
    retrieval = settings.retrieval

    probes = build_probes(query, sub_queries, settings=settings)
    if not probes[0]:
        # An empty query cannot be embedded and has nothing to retrieve. R-23 makes the
        # empty scope abstain downstream, which is the honest outcome, so this is not an
        # error — the same reading `classify_query` applies to the same input.
        return []

    taken = prefetch.take(query=probes[0], filters=filters) if prefetch is not None else None
    if taken is not None and taken.error is not None:
        # R-46(6) at one remove: the original query's failure is the turn's failure, wherever
        # the search happened to run. Re-raised rather than translated, so `handle_node_error`
        # classifies it exactly as it would have without the prefetch.
        raise taken.error

    # The probes still needing a search. When the query arm was prefetched and the router
    # derived nothing — 18 of 20 ordinary questions, per T-304 — this is empty and the turn
    # skips the embedding round trip entirely rather than making an empty one.
    pending = probes[1:] if taken is not None else probes

    # One round trip for every probe, on the query budget (R-46(7)). A failure here is
    # fatal by construction: with no vector there is no dense arm for the original query.
    vectors = await embeddings.embed_queries(pending, model=embedding_model) if pending else []

    searched: list[list[RetrievedChunk] | BaseException] = list(
        await asyncio.gather(
            *(
                _search_one(
                    probe,
                    vector,
                    filters=filters,
                    sessionmaker=sessionmaker,
                    retriever_factory=retriever_factory,
                    top_k=retrieval.fusion_top_k,
                )
                for probe, vector in zip(pending, vectors, strict=True)
            ),
            return_exceptions=True,
        )
    )
    # Index 0 is the original query on both paths, which is what keeps the two rules below —
    # "probe 0's failure is the turn's failure" and "the representative hit comes from the
    # original query" — written once for the prefetched and the unprefetched case alike.
    results: list[list[RetrievedChunk] | BaseException] = (
        [list(taken.hits), *searched] if taken is not None else searched
    )

    lists: list[list[RetrievedChunk]] = []
    failed = 0
    for index, result in enumerate(results):
        if isinstance(result, BaseException):
            if index == 0:
                # The original query. R-45(3) makes every other probe additive; this one is
                # the turn's actual question, so its failure is the turn's failure.
                raise result
            failed += 1
            log.warning(
                PROBE_FAILED,
                probe_index=index,
                error=type(result).__name__,
                error_code=getattr(result, "code", None),
            )
            continue
        lists.append(result)

    by_id: dict[uuid.UUID, RetrievedChunk] = {}
    ranked: list[list[uuid.UUID]] = []
    for hits in lists:
        ranked.append([hit.chunk_id for hit in hits])
        for hit in hits:
            # First writer wins, and the lists are in probe order, so a chunk the original
            # query found is represented by *its* arm ranks and scores rather than by a
            # rewrite's — the diagnostics stay attached to the question the user asked.
            by_id.setdefault(hit.chunk_id, hit)

    merged = rrf_merge(ranked, k=retrieval.rrf_k)
    ordered = sorted(
        (
            # `score` becomes the cross-probe RRF score. Still not user-facing: FR-CIT-04's
            # number is T-306's rerank score, as it was for the per-query RRF it replaces.
            _rescored(by_id[chunk_id], rank.score)
            for chunk_id, rank in merged.items()
        ),
        # Ties broken by id, as `HybridRetriever` does, so an identical turn produces an
        # identical candidate order — a reranker fed a non-deterministic list is untestable.
        key=lambda hit: (-hit.score, hit.chunk_id.bytes),
    )

    # After the merge, never before: R-37(6) — the survivor of an overlapping pair is
    # whichever ranks higher, and per-probe order is not the order that decides that.
    deduped = drop_overlapping_neighbours(ordered) if retrieval.dedupe_adjacent else ordered
    returned = deduped[: retrieval.merged_top_k]

    log.info(
        RETRIEVAL_COMPLETED,
        probes=len(probes),
        # Whether the query arm came from `route` (T-311). The one field that says which of
        # the two paths a turn took, and therefore the only way to read the saving back out
        # of a production log rather than a benchmark.
        prefetched=taken is not None,
        probes_failed=failed,
        candidates=sum(len(hits) for hits in lists),
        merged=len(ordered),
        dropped_overlap=len(ordered) - len(deduped),
        returned=len(returned),
    )
    return returned


def _rescored(hit: RetrievedChunk, score: float) -> RetrievedChunk:
    """`hit` with the merged score. Frozen dataclass, so this is a copy, not a mutation."""
    return dataclasses.replace(hit, score=score)
