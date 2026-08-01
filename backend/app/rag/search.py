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
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

import structlog

from app.config import Settings, get_settings
from app.rag.fusion import drop_overlapping_neighbours, rrf_merge
from app.rag.retrieval import RetrievalFilter, RetrievedChunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.rag.retrieval import Retriever
    from app.services.embeddings import EmbeddingClient

log = structlog.get_logger(__name__)

__all__ = ["build_probes", "retrieve_for_turn"]

#: Telemetry names. `rag.*` rather than `graph.turn.*` on the R-45(8) precedent: the closed
#: `graph.turn.*` vocabulary is a span-pairing contract (R-43(5)) and this is neither end of
#: a span. Counts and codes only — **never** the query, a probe, or chunk text.
RETRIEVAL_COMPLETED = "rag.retrieval.completed"
PROBE_FAILED = "rag.retrieval.probe_failed"


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


async def retrieve_for_turn(
    *,
    query: str,
    sub_queries: Iterable[str] = (),
    filters: RetrievalFilter,
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingClient,
    retriever_factory: Callable[[AsyncSession], Retriever],
    settings: Settings | None = None,
) -> list[RetrievedChunk]:
    """Retrieve for one turn: fan the probes out, merge, dedupe, truncate.

    Raises whatever the embedding call or the **original query's** search raises — the node
    above has no `try`, and FR-RET-05 wants a graceful failure here rather than an answer
    grounded in nothing (R-46(6)).
    """
    settings = settings or get_settings()
    retrieval = settings.retrieval

    probes = build_probes(query, sub_queries, settings=settings)
    if not probes[0]:
        # An empty query cannot be embedded and has nothing to retrieve. R-23 makes the
        # empty scope abstain downstream, which is the honest outcome, so this is not an
        # error — the same reading `classify_query` applies to the same input.
        return []

    # One round trip for every probe, on the query budget (R-46(7)). A failure here is
    # fatal by construction: with no vector there is no dense arm for the original query.
    vectors = await embeddings.embed_queries(probes)

    results = await asyncio.gather(
        *(
            _search_one(
                probe,
                vector,
                filters=filters,
                sessionmaker=sessionmaker,
                retriever_factory=retriever_factory,
                top_k=retrieval.fusion_top_k,
            )
            for probe, vector in zip(probes, vectors, strict=True)
        ),
        return_exceptions=True,
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
