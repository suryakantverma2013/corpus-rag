"""`document_chunks` — chunk rows + pgvector embeddings (FR-ING-03, R-16).

Field set per FR-ING-03: `document_version, chunk_index, chunk_hash,
embedding_fingerprint, token_count, is_active` + KB/tenant scope. `embedding` is a
`VECTOR(3072)` (text-embedding-3-large # TBD(§8.4)); the cosine and FTS indexes are
built in the migration (halfvec-cast HNSW + GIN `to_tsvector`). `meta` (DB column
`metadata`) carries the remaining incremental-embedding fields (embedding model /
version, chunking / preprocessing version).
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import EMBEDDING_DIM, Base, CreatedAtMixin


class DocumentChunk(CreatedAtMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "document_version", "chunk_index"),
        Index(
            "ix_document_chunks_tenant_id_knowledge_base_id_is_active",
            "tenant_id",
            "knowledge_base_id",
            "is_active",
        ),  # FR-RET-04 / NFR-SEC-06 access filter
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    embedding_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'"),  # TBD(OI-21)
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
