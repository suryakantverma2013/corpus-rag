"""Document-chunk repository (T-102, extended for T-205).

Backs incremental embedding (FR-ING-03) and the R-36 version swap. The diff keys on
**`embedding_fingerprint`**, never on `chunk_hash`: the fingerprint subsumes text identity
*and* FR-ING-03's model/chunking/preprocessing re-embed triggers in one comparison, whereas
a hash-keyed diff would miss an embedding-model change entirely (R-35(10)).

Vector similarity search lives behind the retrieval interface (`app.rag.retrieval`), not
here. As everywhere, these methods `flush` and never `commit` — under R-36(3) the commit
*is* the swap, and it belongs to T-207.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Integer, String, Text, column, delete, func, insert, literal, select
from sqlalchemy import values as sa_values
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.base import BaseRepository

#: Rows per carry-forward statement. asyncpg caps a statement at 32,767 bound parameters
#: and each row binds 7, so a 10,000-chunk document (PARSER_MAX_EXTRACTED_CHARS ÷
#: CHUNKER_TARGET_CHARS) would blow the limit in a single VALUES clause.
_CARRY_FORWARD_BATCH = 1_000


@dataclass(frozen=True, slots=True)
class FingerprintSource:
    """An older version's fingerprint whose vector can be carried forward.

    Deliberately not a `DocumentChunk`: the diff that consumes these
    (`app.ingestion.incremental.diff_chunks`) is pure and unit-testable with no database
    and no identity map, the same move `build_chunk_rows` made by taking ids as parameters.

    Carries no vector. Pulling 10,000 × 3072 float4s into Python to decide set membership
    is ~120 MB, and R-36(2)'s whole point is that those bytes never leave the database.
    """

    embedding_fingerprint: str
    document_version: int


@dataclass(frozen=True, slots=True)
class CarryForwardRow:
    """One new-version row whose `embedding` comes from an older version.

    Every field here comes from the **new** chunk. Only `embedding` is sourced from the old
    row — see :meth:`DocumentChunkRepository.carry_forward_embeddings`.
    """

    id: uuid.UUID
    chunk_index: int
    chunk_hash: str
    embedding_fingerprint: str
    token_count: int | None
    chunk_text: str
    meta: dict


@dataclass(frozen=True, slots=True)
class StalePipelineDocument:
    """One `ACTIVE` document whose stored fingerprint inputs no longer match the configured
    pipeline — T-608's unit of work (R-84(2)).

    Metadata only, and deliberately so: R-31(4)'s revisit trigger fires on any surface that
    hands out document *content*, and this one is read by an admin route. No `chunk_text`, no
    `storage_uri`, no chunk id.

    The three `*_drift` flags are what makes a report actionable rather than alarming: they
    name **which** fingerprint input moved, so an operator can tell a deliberate embedding-model
    change from an accidental `CHUNKER_*` retune before paying to re-embed either.
    """

    document_id: uuid.UUID
    owner_id: uuid.UUID
    filename: str
    document_version: int
    chunk_count: int
    token_count: int
    embedding_model_drift: bool
    chunking_version_drift: bool
    preprocessing_version_drift: bool

    @property
    def drifted_inputs(self) -> tuple[str, ...]:
        """The fingerprint inputs that moved, in the formula's own order."""
        return tuple(
            name
            for name, drifted in (
                ("embedding_model", self.embedding_model_drift),
                ("chunking_version", self.chunking_version_drift),
                ("preprocessing_version", self.preprocessing_version_drift),
            )
            if drifted
        )


@dataclass(frozen=True, slots=True)
class StalePipelineTotals:
    """Totals over the **whole** stale set, not the page (T-608).

    `in_flight` is reported rather than folded into `documents` because a document already
    being rebuilt is neither work to do nor work done, and a tool that silently omitted it
    would read as "nothing left" mid-run.
    """

    documents: int
    chunks: int
    token_count: int
    in_flight: int


class DocumentChunkRepository(BaseRepository[DocumentChunk]):
    model = DocumentChunk

    async def add_many(self, chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
        chunks = list(chunks)
        self.session.add_all(chunks)
        await self.session.flush()
        return chunks

    async def list_by_document(self, document_id: uuid.UUID) -> Sequence[DocumentChunk]:
        stmt = select(DocumentChunk).where(DocumentChunk.document_id == document_id)
        return (await self.session.scalars(stmt.order_by(DocumentChunk.chunk_index))).all()

    async def list_by_version(
        self, document_id: uuid.UUID, document_version: int
    ) -> Sequence[DocumentChunk]:
        """Every row of one version, in `chunk_index` order."""
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.document_version == document_version,
            )
            .order_by(DocumentChunk.chunk_index)
        )
        return (await self.session.scalars(stmt)).all()

    # --- FR-ING-03 diff -------------------------------------------------------

    async def fingerprint_sources(
        self, document_id: uuid.UUID, *, exclude_version: int
    ) -> list[FingerprintSource]:
        """Distinct fingerprints of the document's *other* versions that have a vector.

        ``embedding IS NOT NULL`` is applied **here**, not in the caller, so the diff cannot
        be misused. An old row can match on fingerprint while holding a NULL embedding — a
        crashed prior run leaves exactly that — and treating it as unchanged would carry the
        NULL into the new version. `PgVectorRetriever` filters `embedding IS NOT NULL`, so
        that passage would silently disappear from retrieval while the document reported
        `ACTIVE` with a correct `chunk_count`: text the user uploaded, unfindable forever.

        No `is_active` filter (R-36(7) narrows that flag to FR-ING-05 soft-delete, and a
        soft-deleted chunk's vector is still a valid embedding of its text) and no
        `embedding_model` filter (already folded into the fingerprint).
        """
        stmt = (
            select(
                DocumentChunk.embedding_fingerprint,
                func.max(DocumentChunk.document_version).label("document_version"),
            )
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.document_version != exclude_version,
                DocumentChunk.embedding.is_not(None),
            )
            .group_by(DocumentChunk.embedding_fingerprint)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            FingerprintSource(
                embedding_fingerprint=row.embedding_fingerprint,
                document_version=row.document_version,
            )
            for row in rows
        ]

    # --- FR-ING-03 pipeline drift (T-608, R-84(2)) -----------------------------

    def _drift_predicates(
        self, *, embedding_model: str, chunking_version: str, preprocessing_version: str
    ) -> dict[str, object]:
        """`bool_or(stored IS DISTINCT FROM configured)` per fingerprint input.

        **Read, never recomputed.** `document_chunks.metadata` already carries all three
        non-text inputs to `embedding_fingerprint` — R-35 made that payload a contract
        precisely so "why did this row re-embed?" is answerable from the row alone, and this
        is that reader. Recomputing the fingerprint from `chunk_text` would give the same
        answer at the cost of dragging every chunk's text through Python, and would say only
        *that* the document is stale, never *which* input moved.

        **`IS DISTINCT FROM`, not `!=`.** A row whose `metadata` predates the contract (the
        column's server default is `'{}'`) yields SQL NULL for every key, and `NULL != 'x'`
        is NULL — neither true nor false — so a `!=` predicate would silently classify the
        oldest rows in the corpus as fresh. Absent provenance is not evidence of a match.
        """
        meta = DocumentChunk.meta
        return {
            "embedding_model_drift": func.bool_or(
                meta["embedding_model"].astext.is_distinct_from(embedding_model)
            ),
            "chunking_version_drift": func.bool_or(
                meta["chunking_version"].astext.is_distinct_from(chunking_version)
            ),
            "preprocessing_version_drift": func.bool_or(
                meta["preprocessing_version"].astext.is_distinct_from(preprocessing_version)
            ),
        }

    def _stale_grouped(
        self,
        *,
        embedding_model: str,
        chunking_version: str,
        preprocessing_version: str,
        owner_id: uuid.UUID | None,
        in_flight: bool,
    ):  # noqa: ANN202 — a SQLAlchemy Select, whose generic parameters are not worth spelling
        """The grouped drift query both public readers below are built from.

        Joined on ``document_version = documents.current_version`` so the subject is the
        version that is *answering questions*, never a superseded set a crashed swap left
        behind. `ACTIVE` only (R-84(4)): a stale `FAILED` document is already reachable
        through `POST /documents/{id}/retry`, which rebuilds it under the configured pipeline
        by construction, and every other state is either in flight — where a second dispatch
        would race the worker (R-39(5)'s argument) — or on the deletion path.

        ``in_flight`` selects between the two halves of the drifted set: documents with **no**
        open `INGEST` job (the work still to do) and documents with one (already queued). The
        `NOT EXISTS` half is load-bearing rather than hygiene — it is the whole of R-84(9)'s
        resumability, since without it every run re-queues the batch the previous run is still
        rebuilding, and each of those re-queues would burn another version.
        """
        drift = self._drift_predicates(
            embedding_model=embedding_model,
            chunking_version=chunking_version,
            preprocessing_version=preprocessing_version,
        )
        open_job = (
            select(KnowledgeJob.id)
            .where(
                KnowledgeJob.document_id == Document.id,
                KnowledgeJob.job_type == JobType.INGEST,
                KnowledgeJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .exists()
        )
        stmt = (
            select(
                Document.id.label("document_id"),
                Document.owner_id.label("owner_id"),
                Document.filename.label("filename"),
                Document.current_version.label("document_version"),
                Document.created_at.label("created_at"),
                func.count().label("chunk_count"),
                func.coalesce(func.sum(DocumentChunk.token_count), 0).label("token_count"),
                drift["embedding_model_drift"].label("embedding_model_drift"),
                drift["chunking_version_drift"].label("chunking_version_drift"),
                drift["preprocessing_version_drift"].label("preprocessing_version_drift"),
            )
            .join(
                DocumentChunk,
                (DocumentChunk.document_id == Document.id)
                & (DocumentChunk.document_version == Document.current_version),
            )
            .where(
                Document.status == DocumentStatus.ACTIVE,
                Document.deleted_at.is_(None),
                open_job if in_flight else ~open_job,
            )
            .group_by(
                Document.id,
                Document.owner_id,
                Document.filename,
                Document.current_version,
                Document.created_at,
            )
            .having(
                drift["embedding_model_drift"]
                | drift["chunking_version_drift"]
                | drift["preprocessing_version_drift"]
            )
        )
        if owner_id is not None:
            stmt = stmt.where(Document.owner_id == owner_id)
        return stmt

    async def stale_pipeline_documents(
        self,
        *,
        embedding_model: str,
        chunking_version: str,
        preprocessing_version: str,
        owner_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StalePipelineDocument]:
        """Documents whose live version was built by a different pipeline (T-608).

        Ordered ``created_at, id`` — T-108's lesson that timestamps tie, so the id breaks it
        and a bounded run is reproducible rather than merely bounded.
        """
        stmt = self._stale_grouped(
            embedding_model=embedding_model,
            chunking_version=chunking_version,
            preprocessing_version=preprocessing_version,
            owner_id=owner_id,
            in_flight=False,
        )
        stmt = stmt.order_by(Document.created_at, Document.id).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).all()
        return [
            StalePipelineDocument(
                document_id=row.document_id,
                owner_id=row.owner_id,
                filename=row.filename,
                document_version=row.document_version,
                chunk_count=row.chunk_count,
                token_count=row.token_count,
                embedding_model_drift=row.embedding_model_drift,
                chunking_version_drift=row.chunking_version_drift,
                preprocessing_version_drift=row.preprocessing_version_drift,
            )
            for row in rows
        ]

    async def stale_pipeline_totals(
        self,
        *,
        embedding_model: str,
        chunking_version: str,
        preprocessing_version: str,
        owner_id: uuid.UUID | None = None,
    ) -> StalePipelineTotals:
        """Totals over the whole stale set — what a bounded run is a fraction of.

        `token_count` is the FR-ING-03 estimate summed over every chunk that will be
        re-embedded (R-35(7)'s `ceil(len/4)`, so indicative rather than a quotation). It is
        here because "re-embed 4,100 documents" and "spend ~11M embedding tokens" are the same
        sentence to the database and very different sentences to whoever is paying.
        """
        pipeline = {
            "embedding_model": embedding_model,
            "chunking_version": chunking_version,
            "preprocessing_version": preprocessing_version,
            "owner_id": owner_id,
        }
        documents, chunks, tokens = await self._aggregate(**pipeline, in_flight=False)  # type: ignore[arg-type]
        in_flight, _, _ = await self._aggregate(**pipeline, in_flight=True)  # type: ignore[arg-type]
        return StalePipelineTotals(
            documents=documents, chunks=chunks, token_count=tokens, in_flight=in_flight
        )

    async def _aggregate(
        self,
        *,
        embedding_model: str,
        chunking_version: str,
        preprocessing_version: str,
        owner_id: uuid.UUID | None,
        in_flight: bool,
    ) -> tuple[int, int, int]:
        """Roll the grouped query up into (documents, chunks, tokens)."""
        grouped = self._stale_grouped(
            embedding_model=embedding_model,
            chunking_version=chunking_version,
            preprocessing_version=preprocessing_version,
            owner_id=owner_id,
            in_flight=in_flight,
        ).subquery()
        stmt = select(
            func.count(),
            func.coalesce(func.sum(grouped.c.chunk_count), 0),
            func.coalesce(func.sum(grouped.c.token_count), 0),
        ).select_from(grouped)
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def is_pipeline_stale(
        self,
        document_id: uuid.UUID,
        *,
        embedding_model: str,
        chunking_version: str,
        preprocessing_version: str,
    ) -> bool:
        """Whether **this** document's live version was built by a different pipeline.

        The precondition R-84(3) turns into a refusal. Deliberately the same
        `_drift_predicates` the enumeration uses rather than a second reading of the same
        rule: a route that accepted documents the report never lists — or refused ones it did
        — would be the more confusing of the two failures, and two spellings of one predicate
        is how that happens.
        """
        drift = self._drift_predicates(
            embedding_model=embedding_model,
            chunking_version=chunking_version,
            preprocessing_version=preprocessing_version,
        )
        stmt = (
            select(
                drift["embedding_model_drift"]
                | drift["chunking_version_drift"]
                | drift["preprocessing_version_drift"]
            )
            .select_from(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.id == document_id,
                DocumentChunk.document_version == Document.current_version,
            )
        )
        # `bool_or` over an empty set is NULL, which is the honest answer for a document with
        # no chunks at its current version: there is nothing whose provenance could differ,
        # so there is nothing to re-embed. `is True` rather than a truthiness test, because
        # NULL arrives here as `None`.
        return (await self.session.execute(stmt)).scalar() is True

    async def carry_forward_embeddings(
        self,
        rows: Sequence[CarryForwardRow],
        *,
        document_id: uuid.UUID,
        document_version: int,
        knowledge_base_id: uuid.UUID,
        tenant_id: uuid.UUID = DEFAULT_TENANT_ID,  # single-org (OI-21)
    ) -> int:
        """R-36(2): insert reused rows carrying an older version's vector, database-side.

        A naive ``INSERT … SELECT *`` from the old rows is wrong three ways. `chunk_index`
        shifts when a paragraph is inserted; `id` must be freshly minted; and — the one that
        is easy to miss — **`metadata` must come from the new chunk.** A fingerprint pins
        only text + model + versions, so two chunks can share one while differing in
        `block_order`, the character offsets, and the *locator itself*: a paragraph that
        moved from page 3 to page 4 has an identical fingerprint and a different page.
        Carrying the old metadata forward would point a citation at the wrong page of the
        user's own document — an FR-CIT-03 failure no count- or hash-based test can catch.
        **Only `embedding` comes from the old row.**

        `DISTINCT ON` is mandatory rather than an optimisation: `chunk_hash` and therefore
        the fingerprint are not unique within a document (R-35(9)), so N old boilerplate
        rows sharing one fingerprint would fan the join out to N copies of each new row and
        trip `UNIQUE(document_id, document_version, chunk_index)`. The `ORDER BY` makes the
        pick deterministic and prefers the newest surviving vector.

        The join is an inner `JOIN` on purpose. If a source vector vanished between the plan
        read and this insert, the row is *omitted* — which the caller's rowcount check turns
        into a loud, retryable failure — rather than inserted with a NULL embedding, which
        would be silent and permanent.
        """
        if not rows:
            return 0

        table = DocumentChunk.__table__
        source = (
            select(
                DocumentChunk.embedding_fingerprint.label("embedding_fingerprint"),
                DocumentChunk.embedding.label("embedding"),
            )
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.document_version != document_version,
                DocumentChunk.embedding.is_not(None),
            )
            .distinct(DocumentChunk.embedding_fingerprint)
            .order_by(
                DocumentChunk.embedding_fingerprint,
                DocumentChunk.document_version.desc(),
                DocumentChunk.chunk_index,
            )
            .cte("carry_forward_source")
        )

        inserted = 0
        for start in range(0, len(rows), _CARRY_FORWARD_BATCH):
            batch = rows[start : start + _CARRY_FORWARD_BATCH]
            new_rows = sa_values(
                column("id", PGUUID(as_uuid=True)),
                column("chunk_index", Integer),
                column("chunk_hash", String(64)),
                column("embedding_fingerprint", String(64)),
                column("token_count", Integer),
                column("chunk_text", Text),
                column("meta", JSONB),
                name="carry_forward_rows",
            ).data(
                [
                    (
                        row.id,
                        row.chunk_index,
                        row.chunk_hash,
                        row.embedding_fingerprint,
                        row.token_count,
                        row.chunk_text,
                        row.meta,
                    )
                    for row in batch
                ]
            )
            selectable = select(
                new_rows.c.id,
                literal(document_id, type_=PGUUID(as_uuid=True)),
                literal(document_version, type_=Integer),
                new_rows.c.chunk_index,
                new_rows.c.chunk_hash,
                new_rows.c.embedding_fingerprint,
                new_rows.c.token_count,
                literal(tenant_id, type_=PGUUID(as_uuid=True)),
                literal(knowledge_base_id, type_=PGUUID(as_uuid=True)),
                new_rows.c.chunk_text,
                source.c.embedding,
                new_rows.c.meta,
            ).select_from(
                new_rows.join(
                    source,
                    new_rows.c.embedding_fingerprint == source.c.embedding_fingerprint,
                )
            )
            stmt = insert(table).from_select(
                [
                    table.c.id,
                    table.c.document_id,
                    table.c.document_version,
                    table.c.chunk_index,
                    table.c.chunk_hash,
                    table.c.embedding_fingerprint,
                    table.c.token_count,
                    table.c.tenant_id,
                    table.c.knowledge_base_id,
                    table.c.chunk_text,
                    table.c.embedding,
                    table.c.metadata,
                ],
                selectable,
            )
            result = await self.session.execute(stmt)
            inserted += result.rowcount
        await self.session.flush()
        return inserted

    async def count_missing_embeddings(self, document_id: uuid.UUID, document_version: int) -> int:
        """Pre-commit post-condition: a committed version has no NULL embeddings.

        One cheap count closing both write paths — the ORM insert of added rows and the
        carry-forward — on the invariant that actually matters.
        """
        stmt = select(func.count()).where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.document_version == document_version,
            DocumentChunk.embedding.is_(None),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # --- R-36 swap ------------------------------------------------------------

    async def delete_by_version(self, document_id: uuid.UUID, document_version: int) -> int:
        """R-36(5): the FR-ING-04 same-version retry, in one statement.

        Run first and unconditionally at the head of a persist, not only when a retry is
        detected — that is what makes the phase idempotent under a crash at any point,
        including one halfway through its own inserts.
        """
        stmt = delete(DocumentChunk).where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.document_version == document_version,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def delete_other_versions(self, document_id: uuid.UUID, *, keep_version: int) -> int:
        """R-36(4) collect: drop every superseded version of this document.

        Called **inside** the swap transaction. R-36(4) as first written deleted after the
        commit, but retrieval carries no `document_version` predicate
        (`app.db.repositories.retrieval`), so a post-commit delete leaves a window in which
        both versions are searchable — and with no sweeper, a worker that dies in that gap
        makes the duplication permanent. Inside the transaction, MVCC gives the ruling's
        actual guarantee for free: other readers see the old version until the commit and
        only the new one after it, and there is no crash point that orphans a version.
        """
        stmt = delete(DocumentChunk).where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.document_version != keep_version,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    # --- FR-ING-05 ------------------------------------------------------------

    async def delete_by_document(self, document_id: uuid.UUID) -> int:
        """FR-ING-05's "removes vectors": every version's rows, hard-deleted (R-39(3)).

        A hard delete rather than a soft flag: retrieval already excludes the document
        through `documents.searchable`/`deleted_at`, so a flag would change no query result
        while retaining ~12 KB per chunk plus its HNSW entry indefinitely — for a document
        the user asked to delete. That is the same storage argument R-36(4) settled when it
        made the collect step mandatory. `document_chunks.is_active` existed for the soft
        path R-36(7) had imagined; it never gained a writer and was dropped by R-62(3).
        """
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
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
