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

from sqlalchemy import Integer, String, Text, column, delete, func, insert, literal, select, update
from sqlalchemy import values as sa_values
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.db.base import DEFAULT_TENANT_ID
from app.db.models.document_chunk import DocumentChunk
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

    async def carry_forward_embeddings(
        self,
        rows: Sequence[CarryForwardRow],
        *,
        document_id: uuid.UUID,
        document_version: int,
        knowledge_base_id: uuid.UUID,
        tenant_id: uuid.UUID = DEFAULT_TENANT_ID,  # TBD(OI-21)
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
                literal(True),  # noqa: FBT003 — the `is_active` column, not a flag argument
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
                    table.c.is_active,
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

        Not `deactivate` below. R-36(7) had assigned `is_active` exactly this job, but
        retrieval already excludes the document through `documents.searchable`/`deleted_at`,
        so the flag would change no query result while retaining ~12 KB per chunk plus its
        HNSW entry indefinitely — for a document the user asked to delete. That is the same
        storage argument R-36(4) settled when it made the collect step mandatory. `is_active`
        is consequently left with no writer anywhere (OI-28).
        """
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def deactivate(self, chunk_ids: Sequence[uuid.UUID]) -> int:
        """Soft-delete chunks. **Unused** — R-39(3) made FR-ING-05 a hard delete (OI-28)."""
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
