"""Retrieval interface (T-102, OI-18 swap-readiness).

Retrieval sits behind this `Retriever` protocol so the pgvector baseline (R-17) can
be swapped for Milvus without touching callers. All implementations MUST apply
access filters in the query, never post-hoc in Python (FR-RET-04 / NFR-SEC-06).
The concrete `PgVectorRetriever` (dense stage) lives in
`app.db.repositories.retrieval`; hybrid BM25 fusion (T-206) and cross-encoder
rerank (T-306) extend it later.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.db.base import DEFAULT_TENANT_ID


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """Access scope applied inside the retrieval query (FR-RET-04)."""

    tenant_id: uuid.UUID = DEFAULT_TENANT_ID
    knowledge_base_ids: Sequence[uuid.UUID] = field(default_factory=tuple)
    document_ids: Sequence[uuid.UUID] = field(default_factory=tuple)  # explicit / @-mention
    active_only: bool = True


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A single retrieval hit. `score` is stage-dependent (dense: cosine similarity)."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    chunk_text: str
    score: float


@runtime_checkable
class Retriever(Protocol):
    """Vector/hybrid retrieval contract (OI-18)."""

    async def search(
        self,
        query_embedding: Sequence[float],
        *,
        filters: RetrievalFilter,
        top_k: int = 8,  # TBD(§8.4)
    ) -> list[RetrievedChunk]: ...
