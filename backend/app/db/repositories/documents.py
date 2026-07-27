"""Document repository (T-102). Lifecycle + per-KB checksum dedup (FR-KBM-08)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.repositories.base import BaseRepository


class DocumentRepository(BaseRepository[Document]):
    model = Document

    async def list_by_owner(self, owner_id: uuid.UUID) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.owner_id == owner_id, Document.deleted_at.is_(None))
            .order_by(Document.created_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_by_kb(self, knowledge_base_id: uuid.UUID) -> list[Document]:
        stmt = select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.deleted_at.is_(None),
        )
        return list((await self.session.scalars(stmt)).all())

    async def find_by_checksum(
        self, *, knowledge_base_id: uuid.UUID, checksum_sha256: str
    ) -> Document | None:
        """Per-KB dedup lookup (FR-KBM-08); ignores already-deleted rows."""
        stmt = select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.checksum_sha256 == checksum_sha256,
            Document.deleted_at.is_(None),
        )
        return (await self.session.scalars(stmt)).first()

    async def total_bytes_for_owner(self, owner_id: uuid.UUID) -> int:
        """Bytes of stored originals owned by ``owner_id`` — the FR-ERR-02 quota base.

        Counts originals only, never derived artifacts (R-33), and excludes soft-deleted
        rows so freeing space by deleting works immediately rather than after the T-208
        worker finishes. Summed in-query per NFR-SEC-06; `ix_documents_owner_id` covers
        the predicate. `COALESCE` because both an empty set and the nullable column
        yield NULL.
        """
        stmt = select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
            Document.owner_id == owner_id,
            Document.deleted_at.is_(None),
        )
        return int(await self.session.scalar(stmt) or 0)

    async def set_status(
        self, document: Document, status: DocumentStatus, *, error_message: str | None = None
    ) -> Document:
        document.status = status
        if error_message is not None:
            document.error_message = error_message
        await self.session.flush()
        return document

    async def mark_delete_pending(self, document: Document) -> Document:
        """Synchronous deletion gate: DELETE_PENDING + not searchable (FR-ING-05)."""
        document.status = DocumentStatus.DELETE_PENDING
        document.searchable = False
        await self.session.flush()
        return document

    async def mark_deleted(self, document: Document) -> Document:
        document.status = DocumentStatus.DELETED
        document.deleted_at = datetime.now(UTC)
        await self.session.flush()
        return document
