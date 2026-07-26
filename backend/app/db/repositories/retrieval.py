"""pgvector dense retriever (T-102) — the OI-18 `Retriever` baseline impl.

Dense cosine similarity over `document_chunks.embedding`, with every access filter
applied in the query (FR-RET-04 / NFR-SEC-06). This is the first stage only; hybrid
BM25 fusion (T-206) and cross-encoder rerank (T-306) build on it. The displayed
citation score becomes the rerank score once T-306 lands (FR-CIT-04).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document_chunk import DocumentChunk
from app.rag.retrieval import RetrievalFilter, RetrievedChunk


class PgVectorRetriever:
    """Concrete `Retriever` (see `app.rag.retrieval.Retriever`)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        query_embedding: Sequence[float],
        *,
        filters: RetrievalFilter,
        top_k: int = 8,  # TBD(§8.4)
    ) -> list[RetrievedChunk]:
        distance = DocumentChunk.embedding.cosine_distance(list(query_embedding)).label("distance")
        stmt = select(DocumentChunk, distance).where(
            DocumentChunk.tenant_id == filters.tenant_id,
            DocumentChunk.embedding.is_not(None),
        )
        if filters.active_only:
            stmt = stmt.where(DocumentChunk.is_active.is_(True))
        if filters.knowledge_base_ids:
            stmt = stmt.where(DocumentChunk.knowledge_base_id.in_(list(filters.knowledge_base_ids)))
        if filters.document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(list(filters.document_ids)))
        stmt = stmt.order_by(distance).limit(top_k)

        rows = (await self.session.execute(stmt)).all()
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                chunk_text=chunk.chunk_text,
                score=1.0 - float(dist),  # cosine similarity
            )
            for chunk, dist in rows
        ]
