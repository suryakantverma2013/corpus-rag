"""Document-chunk repository (T-102).

Backs incremental embedding (FR-ING-03): `active_hash_map` returns the current
active chunks' content hashes so the ingestion diff can compute added/deleted/
unchanged by set difference. Vector similarity search lives behind the retrieval
interface (`app.rag.retrieval`), not here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import select, update

from app.db.models.document_chunk import DocumentChunk
from app.db.repositories.base import BaseRepository


class DocumentChunkRepository(BaseRepository[DocumentChunk]):
    model = DocumentChunk

    async def add_many(self, chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
        chunks = list(chunks)
        self.session.add_all(chunks)
        await self.session.flush()
        return chunks

    async def list_by_document(
        self, document_id: uuid.UUID, *, active_only: bool = True
    ) -> Sequence[DocumentChunk]:
        stmt = select(DocumentChunk).where(DocumentChunk.document_id == document_id)
        if active_only:
            stmt = stmt.where(DocumentChunk.is_active.is_(True))
        return (await self.session.scalars(stmt.order_by(DocumentChunk.chunk_index))).all()

    async def active_hash_map(self, document_id: uuid.UUID) -> dict[uuid.UUID, str]:
        """`{chunk_id: chunk_hash}` for the document's active chunks (FR-ING-03)."""
        stmt = select(DocumentChunk.id, DocumentChunk.chunk_hash).where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.is_active.is_(True),
        )
        return {row.id: row.chunk_hash for row in (await self.session.execute(stmt)).all()}

    async def deactivate(self, chunk_ids: Sequence[uuid.UUID]) -> int:
        if not chunk_ids:
            return 0
        stmt = update(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids)).values(is_active=False)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def delete_by_ids(self, chunk_ids: Sequence[uuid.UUID]) -> int:
        if not chunk_ids:
            return 0
        stmt = select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
        for chunk in (await self.session.scalars(stmt)).all():
            await self.session.delete(chunk)
        await self.session.flush()
        return len(chunk_ids)
