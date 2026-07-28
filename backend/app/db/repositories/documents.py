"""Document repository (T-102). Lifecycle + per-KB checksum dedup (FR-KBM-08).

T-209 adds the FR-KBM-03/09 read surface: `list_for_owner` / `get_listing_scoped`, which
return a `DocumentListing` (document + its knowledge base's scope + its newest job's
identity) rather than a bare `Document`, so the API layer never has to issue a follow-up
query per row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, true

from app.db.enums import DocumentStatus, KBVisibility
from app.db.models.document import Document
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class DocumentListing:
    """One row of the FR-KBM-04 modal / FR-KBM-09 table.

    A dataclass rather than a raw `Row` so the API layer does not depend on column
    ordering, and so `list` and `get` are provably the same shape.

    The job fields are the FR-ING-06 back-reference required by R-40(6): without them no
    endpoint hands out a job id, and `GET /jobs/{id}` — the only place a failure's
    diagnostics live, and the only surface on which a dead-lettered purge is visible at
    all (R-39(7)) — is unreachable from the row that needs it. The job's *status* is
    deliberately absent: `GET /jobs/{id}` owns that, and a surface rendering two statuses
    read at two different moments will eventually render them disagreeing.
    """

    document: Document
    scope: KBVisibility
    conversation_id: uuid.UUID | None
    latest_job_id: uuid.UUID | None
    latest_job_error_code: str | None
    latest_job_document_version: int | None


def _latest_job_lateral():
    """`LEFT JOIN LATERAL (… LIMIT 1) ON true` — the newest job per document, in one query.

    A lateral rather than three correlated scalar subqueries (three index probes, and
    nothing makes them agree on *which* job they picked) and rather than a per-row
    follow-up read, which is the N+1 this exists to avoid.

    **`created_at DESC, id DESC`, not `created_at DESC` alone.** `now()` is the
    *transaction* timestamp (T-108), so two jobs written in one transaction tie exactly and
    an untied `LIMIT 1` picks differently on each execution — the same class of flake T-108
    was raised for. The `id` tiebreak is arbitrary but total and stable, which is all this
    needs.

    Deliberately **not** ordered by `document_version DESC`: a DELETE job carries whatever
    version was live when it was requested, so a version-first ordering would rank a queued
    replace above the deletion that superseded it.
    """
    return (
        select(
            KnowledgeJob.id.label("latest_job_id"),
            KnowledgeJob.error_code.label("latest_job_error_code"),
            KnowledgeJob.document_version.label("latest_job_document_version"),
        )
        .where(KnowledgeJob.document_id == Document.id)
        .order_by(KnowledgeJob.created_at.desc(), KnowledgeJob.id.desc())
        .limit(1)
        .correlate(Document)
        .lateral("latest_job")
    )


def _listing_select() -> Select:
    """The shared `DocumentListing` projection used by both list and get."""
    lat = _latest_job_lateral()
    return (
        select(
            Document,
            KnowledgeBase.visibility,
            KnowledgeBase.conversation_id,
            lat.c.latest_job_id,
            lat.c.latest_job_error_code,
            lat.c.latest_job_document_version,
        )
        .select_from(Document)
        # INNER: `documents.knowledge_base_id` is NOT NULL behind a foreign key, so this
        # drops nothing and an outer join would only hide a broken reference.
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        # OUTER, and not defensively: a document whose job row was never written must still
        # list. An inner join here returns an empty page, which reads like an auth bug.
        .outerjoin(lat, true())
    )


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

    async def list_for_owner(
        self,
        *,
        owner_id: uuid.UUID,
        knowledge_base_id: uuid.UUID | None = None,
        status: DocumentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DocumentListing]:
        """The FR-KBM-03/09 page (R-40(5)). Every filter is in-query per NFR-SEC-06.

        `deleted_at IS NULL` is the live filter and is exactly the right predicate rather
        than `status != DELETED`: `mark_deleted` writes both in one transaction, and this
        form keeps `DELETE_PENDING`/`DELETING` **visible**, which FR-KBM-04 requires
        (`Deleting` is one of its eight labels) and R-39(7) makes load-bearing — a purge
        that exhausts its retries parks at `DELETING` indefinitely and must stay on screen
        to be re-driven by a repeat `DELETE`.

        Ordered `created_at DESC, id DESC`. The tiebreak is mandatory, not defensive:
        `now()` is the transaction timestamp, so any rows written in one transaction tie
        exactly and the order would otherwise fall through to a random UUID (T-108). Sorted
        on `created_at` and **not** `updated_at`, despite FR-KBM-09 calling its column
        "Last updated" — `updated_at` moves on every one of the six FR-ING-01 stage writes,
        so rows would leapfrog each other repeatedly in a view FR-KBM-09 wants live.
        """
        stmt = _listing_select().where(
            Document.owner_id == owner_id,
            Document.deleted_at.is_(None),
        )
        if knowledge_base_id is not None:
            stmt = stmt.where(Document.knowledge_base_id == knowledge_base_id)
        if status is not None:
            stmt = stmt.where(Document.status == status)
        stmt = (
            stmt.order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [DocumentListing(*row) for row in (await self.session.execute(stmt)).all()]

    async def get_listing_scoped(
        self, document_id: uuid.UUID, *, owner_id: uuid.UUID | None
    ) -> DocumentListing | None:
        """`GET /documents/{id}` — owner-or-admin, `owner_id=None` being the admin path.

        Same shape as `list_for_owner` minus one predicate: **no `deleted_at IS NULL`**. A
        tombstone is returned with its terminal state rather than 404'd, because a client
        that has just received `DELETE`'s `202` and polls the document must see `DELETED`,
        not a sudden `404` it cannot tell apart from a wrong id. The list excludes it; this
        does not (R-40(5)).

        `owner_id=None` ⇒ no owner predicate is the same idiom as
        `KnowledgeJobRepository.get_scoped`; a foreign document returns `None`, which the
        route renders `404` and never `403` (NFR-SEC-02, R-39(1)).
        """
        stmt = _listing_select().where(Document.id == document_id)
        if owner_id is not None:
            stmt = stmt.where(Document.owner_id == owner_id)
        row = (await self.session.execute(stmt)).first()
        return DocumentListing(*row) if row is not None else None

    async def get_for_update(self, id_: uuid.UUID) -> Document | None:
        """Load one document holding a row lock for the rest of the transaction (R-39(8)).

        The serialisation point between deletion and ingestion. `session.get` would return
        the identity map's copy without touching the database, which is precisely the wrong
        thing here: the ingest swap needs to observe a *concurrent* transaction's
        `DELETE_PENDING` before it commits `searchable = True` over it, and the delete
        request needs the ingest worker to block rather than interleave. `populate_existing`
        makes the re-read overwrite the stale in-session copy.
        """
        stmt = (
            select(Document)
            .where(Document.id == id_)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(stmt)).first()

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
        self,
        document: Document,
        status: DocumentStatus,
        *,
        error_message: str | None = None,
        clear_error: bool = False,
    ) -> Document:
        """Move a document to `status`.

        `error_message=None` means "leave it alone", so `clear_error=True` is the explicit
        opt-in to wipe it — used by the FR-ING-06 retry, where a document going back to
        `QUEUED` must stop showing the reason its last attempt failed. Same idiom as
        `KnowledgeJobRepository.update_status`.
        """
        document.status = status
        if clear_error:
            document.error_message = None
        if error_message is not None:
            document.error_message = error_message
        await self.session.flush()
        return document

    async def mark_active(
        self,
        document: Document,
        *,
        chunk_count: int,
        current_version: int,
        page_count: int | None = None,
    ) -> Document:
        """The end of a successful ingestion (T-207) — five writes that must land together.

        The caller commits this in the **same transaction** as `persist_chunk_set`, and
        that commit *is* R-36(3)'s swap: readers see the previous version until it lands
        and only the new one after.

        **`current_version` is not bookkeeping.** T-206's retrieval query filters
        `document_chunks.document_version = documents.current_version` (R-37(9)), and
        `persist_chunk_set` deliberately does not touch the pointer. Advance it here or a
        re-ingested document reaches `ACTIVE`, lists in the KB modal, and matches nothing
        at all — a failure that is invisible until someone asks a question about it.

        `error_message` is cleared: a document that failed, was retried and now serves must
        not keep showing the old reason in the FR-KBM-04 surface.

        **`page_count` is assigned unconditionally** (R-40(7)), including to `None`. The
        earlier "only when not null" form was invisible until replace existed: R-40 lets a
        replace change the file's format, so a 58-page PDF replaced by a CSV would swap in
        a version whose parser yields no page count while the column kept the PDF's, and
        FR-KBM-04 would render "58 pages" for a CSV forever. Safe because the only caller
        always passes `parsed.page_count` for the version it is swapping in, and a
        same-version retry re-parses the same bytes to the same answer.
        """
        document.status = DocumentStatus.ACTIVE
        document.chunk_count = chunk_count
        document.current_version = current_version
        document.page_count = page_count
        # FR-RET-04: nothing retrieves a document until it is genuinely queryable.
        document.searchable = True
        document.error_message = None
        await self.session.flush()
        return document

    async def mark_delete_pending(self, document: Document) -> Document:
        """Synchronous deletion gate: DELETE_PENDING + not searchable (FR-ING-05).

        This flush is the whole of FR-ING-05's "leaves retrieval immediately" — the caller
        commits it before returning `202`, and `_access_predicates` filters `searchable`, so
        the very next query cannot see the document however long the purge then takes.
        """
        document.status = DocumentStatus.DELETE_PENDING
        document.searchable = False
        await self.session.flush()
        return document

    async def mark_deleted(self, document: Document) -> Document:
        """Terminal state of the purge (FR-ING-05), written in the worker's final txn.

        `chunk_count` is zeroed because R-39(3) hard-deletes the rows it counted — leaving
        it would have the FR-KBM-09 documents view report chunks for a document that has
        none. The row itself survives: FR-ING-05 requires the `DELETED` + timestamp record,
        `knowledge_jobs.document_id` references it, and the FR-KBM-08 partial index (R-39(4))
        is what keeps the tombstone from blocking a re-upload.
        """
        document.status = DocumentStatus.DELETED
        document.deleted_at = datetime.now(UTC)
        document.chunk_count = 0
        await self.session.flush()
        return document
