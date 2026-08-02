"""The query-arm handoff between `route` and `retrieve` (T-311).

`route` and `retrieve` are sequential edges, so a turn sits idle for the router's ~840 ms
before it touches the database. It need not: **R-45(3) makes probes additive** — T-305
always searches the original query itself — so the query's embedding and its dense/sparse
arms depend on nothing the router produces. `route` therefore starts that one arm
concurrently with `classify_query` and leaves the result here; `retrieve` picks it up and
runs only the *derived* probes.

**Why a slot on `RAGContext` and not a state channel.** The merge needs `RetrievedChunk`
objects, not ids: R-46(3)'s second RRF pass ranks them and R-37(6)'s overlap dedupe keys on
`(document_id, block_order, block_chunk_index)` out of `meta`. Chunk text in `RAGState` is
exactly what FR-PER-03 forbids and what `tests/test_graph.py` rejects — so the hits travel
in memory, on the run-scoped context that is never checkpointed (R-42(3)), and **no
`RAGState` field is added**.

That makes this a cache with exactly one safe failure mode, and the whole design is bent
toward it: **an empty or mismatched slot is always correct**, because the consumer then
searches the query itself, which is what every turn did before this module existed. So:

* **Single use.** :meth:`QueryArmPrefetch.take` clears the slot before it returns anything —
  including when it returns ``None``. The `adapt` back edge re-enters `retrieve` without
  re-running `route`, and a second pass must search the query again beside its new HyDE
  probe rather than replay a result the first pass already merged.
* **Keyed on both the query and the scope.** A hit is served only when the probe text *and*
  the `RetrievalFilter` match what the arm actually ran under. The filter is a frozen
  dataclass, so this is an equality test, and it is the reason a turn whose
  `@`-mentions differ can never be handed another scope's rows (R-46(1)).
* **A failure is carried, not swallowed.** The producer never raises — it runs inside
  `route`, which fails *open* (R-45(2)) — so the exception rides in the slot and is
  re-raised by the consumer at `retrieve`, which fails *closed* (R-46(6)). The direction
  each node fails in is unchanged by the optimisation, which is the point.

Imports no langgraph, like `errors.py`, `history.py`, `router.py` and `search.py`; with
``from __future__ import annotations`` it needs no runtime import at all, so `state.py` can
hold one of these without pulling the retrieval stack into the state contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from app.rag.retrieval import RetrievalFilter, RetrievedChunk

__all__ = ["QueryArmPrefetch", "QueryArmResult"]


@dataclass(frozen=True, slots=True)
class QueryArmResult:
    """One completed query arm: its hits, or the exception that stopped it.

    Exactly one of the two is meaningful, and ``error`` wins — an arm that raised has no
    partial result worth grounding in.
    """

    hits: tuple[RetrievedChunk, ...] = ()
    error: BaseException | None = None


@dataclass(slots=True)
class QueryArmPrefetch:
    """A single-slot, single-use handoff for the original query's retrieval arm.

    Run-scoped and mutable, which is unusual in this package and deliberate: it is the one
    thing in a turn that must cross a superstep boundary without being checkpointed. It is
    default-constructed on every :class:`~app.rag.state.RAGContext`, so a caller opts in by
    doing nothing, and a caller that reuses one context across turns is still safe — the
    key check and the single-use pop between them mean the worst case is a wasted search.

    Not thread- or task-safe, and does not need to be: one turn writes it from one node and
    reads it from one node, and the two never run at the same time.
    """

    query: str | None = None
    filters: RetrievalFilter | None = None
    result: QueryArmResult | None = None

    def publish(
        self,
        *,
        query: str,
        filters: RetrievalFilter,
        hits: Sequence[RetrievedChunk] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Record what the arm produced, against the exact input it ran on."""
        self.query = query
        self.filters = filters
        self.result = QueryArmResult(hits=tuple(hits or ()), error=error)

    def take(self, *, query: str, filters: RetrievalFilter) -> QueryArmResult | None:
        """Consume the slot, or ``None`` if it is empty or was filled for something else.

        Clears unconditionally. A slot that does not match this call is stale by definition
        — nothing later in the turn can match it either — and leaving it behind is how a
        cache keyed on the wrong thing eventually answers the wrong question.
        """
        stored_query, stored_filters, result = self.query, self.filters, self.result
        self.query = self.filters = self.result = None
        if result is None or stored_query != query or stored_filters != filters:
            return None
        return result
