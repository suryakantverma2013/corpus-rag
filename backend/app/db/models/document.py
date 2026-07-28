"""`documents` — per-document lifecycle + metadata (§4.15, FR-ING/FR-KBM).

`status` walks the 11-state machine (FR-ING-01); `searchable` is the synchronous
deletion gate (FR-ING-05); `checksum_sha256` is unique per knowledge base **among
live rows** for dedup (FR-KBM-08 / R-39(4) — a deleted document must not block
re-uploading the same file). `tenant_id`/`owner_id`/`knowledge_base_id`/`searchable` back the
FR-RET-04 in-query access filter. Original bytes live in object storage
(`storage_uri`); this row is metadata only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.enums import DocumentStatus, str_enum


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # FR-KBM-08 per-KB dedup — **partial**, over live rows only (R-39(4)).
        #
        # `DocumentRepository.find_by_checksum` has always filtered `deleted_at IS NULL`,
        # so once FR-ING-05 started writing that column a full constraint and the code
        # disagreed: the dedup fast path skips the tombstone, the insert trips the
        # constraint anyway, and T-202's dedup-race branch re-reads with the same filter,
        # finds nothing and raises — turning "re-upload a file I previously deleted" into
        # a 503. The predicate here is exactly the one the query uses.
        Index(
            "uq_documents_knowledge_base_id_checksum_sha256_live",
            "knowledge_base_id",
            "checksum_sha256",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_documents_tenant_id_knowledge_base_id_searchable",
            "tenant_id",
            "knowledge_base_id",
            "searchable",
        ),  # FR-RET-04 / NFR-SEC-06 access filter
        Index("ix_documents_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'"),  # TBD(OI-21)
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Size of the stored original. Summed per owner for the FR-ERR-02 10 GB quota
    # (T-202); nullable so pre-T-202 rows and future backfills stay valid.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[DocumentStatus] = mapped_column(str_enum(DocumentStatus), nullable=False)
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
