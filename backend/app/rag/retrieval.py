"""Retrieval interface (T-102, OI-18 swap-readiness; extended by T-206).

Retrieval sits behind this `Retriever` protocol so the pgvector baseline (R-17) can
be swapped for Milvus without touching callers. All implementations MUST apply
access filters in the query, never post-hoc in Python (FR-RET-04 / NFR-SEC-06).

The concrete implementations live in `app.db.repositories.retrieval`: `HybridRetriever`
(the FR-RET-01 committed default — dense + sparse, fused by RRF per R-37) and
`PgVectorRetriever` (the dense-only fallback FR-RET-01 sanctions). Cross-encoder rerank
(T-306) consumes what these return and produces the FR-CIT-04 score.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.db.base import DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """Access scope applied inside the retrieval query (FR-RET-04).

    ``owner_id`` has **no default on purpose**. FR-RET-04 names tenant *and owner*, and
    under R-25/OI-15 every knowledge base belongs to exactly one user, so a retrieval with
    no owner scope reads another user's documents. A default here would let a call site
    omit it and still compile; requiring it makes every caller state the scope out loud.
    """

    owner_id: uuid.UUID
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID
    knowledge_base_ids: Sequence[uuid.UUID] = field(default_factory=tuple)
    document_ids: Sequence[uuid.UUID] = field(default_factory=tuple)  # explicit / @-mention
    active_only: bool = True


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A single retrieval hit.

    ``score`` is stage-dependent: RRF for `HybridRetriever` (R-37(7)), cosine similarity for
    the dense-only `PgVectorRetriever`. **Neither is user-facing** — per §10.1 and FR-CIT-04
    the number in the hover card is the T-306 cross-encoder rerank score. The per-arm ranks
    and raw scores are carried for debugging and as rerank features; either is ``None`` when
    that arm did not return the chunk.

    ``meta`` is the R-35(12) `document_chunks.metadata` payload, which is where the R-34
    ``locator`` lives — FR-CIT-03 renders the citation from it, so returning it here saves
    T-305 a second read of every chunk it just fetched.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    chunk_index: int
    chunk_text: str
    score: float
    meta: Mapping[str, Any] = field(default_factory=dict)
    dense_rank: int | None = None
    sparse_rank: int | None = None
    dense_score: float | None = None
    sparse_score: float | None = None

    @property
    def block_order(self) -> int | None:
        """R-35(12) block ordinal — the overlap-dedupe key half that `chunk_index` is not."""
        value = self.meta.get("block_order")
        return value if isinstance(value, int) else None

    @property
    def block_chunk_index(self) -> int | None:
        """R-35(12) ordinal within the block. See `app.rag.fusion.drop_overlapping_neighbours`."""
        value = self.meta.get("block_chunk_index")
        return value if isinstance(value, int) else None


@runtime_checkable
class Retriever(Protocol):
    """Vector/hybrid retrieval contract (OI-18).

    Takes the query **text and its embedding**: the sparse arm needs the words, the dense
    arm needs the vector, and any OI-18 replacement store would need both. Embedding is
    deliberately the caller's job (R-37(8)) — it keeps network I/O out of the repository
    layer, and it puts `EmbeddingInputError` (an over-long query is user input) where it can
    be rendered as a `400` rather than surfacing from inside a database call.
    """

    async def search(
        self,
        query_text: str,
        query_embedding: Sequence[float],
        *,
        filters: RetrievalFilter,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]: ...
