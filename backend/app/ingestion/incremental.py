"""Incremental embedding: the FR-ING-03 diff and the R-36 version swap (T-205).

T-204 produces a complete :class:`~app.ingestion.chunker.ChunkedDocument` in which every
chunk carries an `embedding_fingerprint` but no vector. This module decides which of those
chunks actually need an embedding, buys only those, and writes the version's complete chunk
set — carrying every unchanged vector across inside the database.

**The diff keys on `embedding_fingerprint`, not `chunk_hash`** (R-35(10)). The fingerprint
is ``SHA-256(chunk_text | embedding_model | chunking_version | preprocessing_version)``, so
one set operation covers text identity *and* every FR-ING-03 re-embed trigger; a hash-keyed
diff would miss an embedding-model change entirely and re-embed nothing when it must
re-embed everything.

**Persistence is copy → swap → collect** (R-36). A version bump never re-stamps a surviving
row: it writes a complete self-contained set at ``(document_id, v_new, 0..n)``, carries
unchanged vectors forward with an ``INSERT … SELECT``, and drops the superseded version — all
inside the caller's single transaction, so the previous version keeps serving retrieval
until the swap commits and no crash point can leave a document answering from a mixture of
two versions.

The two entry points are split at the transaction boundary on purpose:
:func:`plan_chunk_set` reads and calls OpenAI but writes nothing;
:func:`persist_chunk_set` writes but touches no network. Holding a write transaction open
across the embedding calls would pin the cluster's `xmin` horizon for the minutes a large
document can take, and would be killed outright by any `idle_in_transaction_session_timeout`
— after the embeddings had already been paid for.

Neither function commits, and neither touches `documents.status`, `documents.chunk_count`,
`documents.searchable` or `knowledge_jobs`: the FR-ING-01 transitions and the commit that
*is* the swap belong to T-207.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.models.document_chunk import DocumentChunk
from app.db.repositories.chunks import (
    CarryForwardRow,
    DocumentChunkRepository,
    FingerprintSource,
)
from app.ingestion.chunker import Chunk, ChunkedDocument, build_chunk_rows
from app.services.embeddings import EmbeddingClient

log = structlog.get_logger(__name__)

__all__ = [
    "ChunkDiff",
    "ChunkSetPlan",
    "ChunkSetResult",
    "IncrementalError",
    "diff_chunks",
    "persist_chunk_set",
    "plan_chunk_set",
]


# --- errors -------------------------------------------------------------------


class IncrementalError(Exception):
    """A chunk set could not be written.

    Same shape as `ParseError` and `EmbeddingError`, so T-207 reads `code` and `retryable`
    uniformly across all three taxonomies.
    """

    code: ClassVar[str] = "INCREMENTAL_FAILED"
    retryable: ClassVar[bool] = False


class ChunkCarryForwardError(IncrementalError):
    """Fewer rows were carried forward than the plan expected.

    A source vector disappeared between the plan read and the insert — a concurrent
    deletion, say. Retryable: a re-run re-diffs against what is actually there and honestly
    re-embeds whatever no longer has a source.
    """

    code: ClassVar[str] = "CHUNK_CARRY_INCOMPLETE"
    retryable: ClassVar[bool] = True


class MissingEmbeddingError(IncrementalError):
    """The written version contains a row with no vector.

    Never commit this. `PgVectorRetriever` filters `embedding IS NOT NULL`, so such a row is
    a passage the user uploaded that retrieval can never return and no status field would
    ever reveal.
    """

    code: ClassVar[str] = "CHUNK_EMBEDDING_MISSING"
    retryable: ClassVar[bool] = True


# --- diff ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkDiff:
    """Which of the new version's chunks must be embedded, and which can be reused."""

    reused: tuple[Chunk, ...]  # ascending `chunk_index`; carried DB-side, zero API calls
    added: tuple[Chunk, ...]  # ascending `chunk_index`; must be embedded
    obsolete_fingerprints: int  # old fingerprints absent from the new set (diagnostics)

    @property
    def total(self) -> int:
        return len(self.reused) + len(self.added)


def diff_chunks(new_chunks: Sequence[Chunk], sources: Iterable[FingerprintSource]) -> ChunkDiff:
    """Split the new chunk set into reusable and to-be-embedded. Pure — no session, no ORM.

    The available fingerprints are held in a **set** and tested by membership, never
    consumed. That is the correct structure precisely because `chunk_hash`, and therefore
    the fingerprint, is not unique within a document (R-35(9)): repeated boilerplate,
    per-section disclaimers and duplicated CSV row groups all collide legitimately. A
    consuming match (`dict.pop`, a `Counter` decrement) would classify the first occurrence
    as reused and occurrences 2..N as added, re-embedding text whose vector is already
    stored — and doing so non-deterministically with respect to which copy wins. There is
    nothing to consume: an embedding is a pure function of text, so one source vector serves
    N new rows.

    Callers must obtain ``sources`` from
    :meth:`~app.db.repositories.chunks.DocumentChunkRepository.fingerprint_sources`, which
    is where the ``embedding IS NOT NULL`` guard lives — a fingerprint match against a row
    with no vector is not a reusable chunk.
    """
    available = {source.embedding_fingerprint for source in sources}
    reused: list[Chunk] = []
    added: list[Chunk] = []
    for chunk in new_chunks:
        (reused if chunk.embedding_fingerprint in available else added).append(chunk)
    seen = {chunk.embedding_fingerprint for chunk in new_chunks}
    return ChunkDiff(
        reused=tuple(reused),
        added=tuple(added),
        obsolete_fingerprints=len(available - seen),
    )


# --- plan ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkSetPlan:
    """Everything needed to write one version. No session, no network, no pending I/O."""

    document_id: uuid.UUID
    document_version: int
    knowledge_base_id: uuid.UUID
    tenant_id: uuid.UUID
    embedding_model: str
    added_rows: tuple[DocumentChunk, ...]  # unsaved, `embedding` already populated
    reused_rows: tuple[CarryForwardRow, ...]  # `embedding` filled database-side
    embedded_inputs: int  # texts actually sent after dedupe — what you paid for

    @property
    def total(self) -> int:
        """Reused + added — `documents.chunk_count` for this version (R-35(13))."""
        return len(self.added_rows) + len(self.reused_rows)


async def plan_chunk_set(
    *,
    session: AsyncSession,
    client: EmbeddingClient,
    chunked: ChunkedDocument,
    document_id: uuid.UUID,
    document_version: int,
    knowledge_base_id: uuid.UUID,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,  # single-org (OI-21)
) -> ChunkSetPlan:
    """Diff against the stored versions and embed what changed. **Reads only.**

    Writes nothing and takes no locks, so the caller can (and should) let the read snapshot
    go before opening the write transaction — see the module docstring.
    """
    repo = DocumentChunkRepository(session)
    sources = await repo.fingerprint_sources(document_id, exclude_version=document_version)
    diff = diff_chunks(chunked.chunks, sources)

    # Dedupe the embedding *calls* (R-35(9)) — never the rows, whose identity is
    # `chunk_index`. Keyed on the fingerprint rather than the hash: within one
    # ChunkedDocument the model and both version strings are constant, so the two keys are
    # in bijection here, and using one key throughout the module beats two that are equal
    # only under an unstated assumption.
    unique: dict[str, str] = {}
    for chunk in diff.added:
        unique.setdefault(chunk.embedding_fingerprint, chunk.text)

    vectors = await client.embed_texts(list(unique.values())) if unique else []
    by_fingerprint = dict(zip(unique.keys(), vectors, strict=True))

    added_rows = build_chunk_rows(
        replace(chunked, chunks=diff.added),
        document_id=document_id,
        document_version=document_version,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
    )
    for row, chunk in zip(added_rows, diff.added, strict=True):
        row.embedding = by_fingerprint[chunk.embedding_fingerprint]

    reused_rows = tuple(
        CarryForwardRow(
            id=uuid.uuid4(),
            chunk_index=chunk.chunk_index,
            chunk_hash=chunk.chunk_hash,
            embedding_fingerprint=chunk.embedding_fingerprint,
            token_count=chunk.token_count,
            chunk_text=chunk.text,
            meta=chunked.meta_for(chunk),
        )
        for chunk in diff.reused
    )

    log.info(
        "incremental.planned",
        document_id=str(document_id),
        document_version=document_version,
        reused=len(diff.reused),
        added=len(diff.added),
        embedded_inputs=len(unique),
        obsolete_fingerprints=diff.obsolete_fingerprints,
    )
    return ChunkSetPlan(
        document_id=document_id,
        document_version=document_version,
        knowledge_base_id=knowledge_base_id,
        tenant_id=tenant_id,
        embedding_model=chunked.embedding_model,
        added_rows=tuple(added_rows),
        reused_rows=reused_rows,
        embedded_inputs=len(unique),
    )


# --- persist ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkSetResult:
    reused: int
    added: int
    deleted_retry_rows: int  # rows a previous attempt at this same version had left behind
    collected: int  # superseded versions' rows dropped by the swap

    @property
    def total(self) -> int:
        """→ `documents.chunk_count` (R-35(13)): reused + added, not added only."""
        return self.reused + self.added


async def persist_chunk_set(session: AsyncSession, *, plan: ChunkSetPlan) -> ChunkSetResult:
    """Write the complete self-contained set at ``(document_id, v_new, 0..n)`` (R-36).

    **Does not commit.** The caller (T-207) commits this together with the FR-ING-01
    transition to `ACTIVE`, and that commit *is* R-36(3)'s swap.

    The collect step is performed here, inside the same transaction, rather than being left
    to the caller — see
    :meth:`~app.db.repositories.chunks.DocumentChunkRepository.delete_other_versions` for
    why the atomic form is the one that holds the ruling's guarantee. Keeping it here means
    a caller cannot forget it and leave two versions searchable.
    """
    repo = DocumentChunkRepository(session)

    # First and unconditional, not only on a detected retry (R-36(5)). This one statement
    # is what makes the whole phase idempotent under a crash at any point — including one
    # part-way through its own inserts.
    deleted = await repo.delete_by_version(plan.document_id, plan.document_version)

    if plan.added_rows:
        await repo.add_many(plan.added_rows)

    carried = await repo.carry_forward_embeddings(
        plan.reused_rows,
        document_id=plan.document_id,
        document_version=plan.document_version,
        knowledge_base_id=plan.knowledge_base_id,
        tenant_id=plan.tenant_id,
    )
    if carried != len(plan.reused_rows):
        raise ChunkCarryForwardError(
            f"carried {carried} of {len(plan.reused_rows)} reused chunks for document "
            f"{plan.document_id} v{plan.document_version}; a source vector disappeared"
        )

    missing = await repo.count_missing_embeddings(plan.document_id, plan.document_version)
    if missing:
        raise MissingEmbeddingError(
            f"{missing} chunk(s) of document {plan.document_id} v{plan.document_version} "
            "have no embedding"
        )

    collected = await repo.delete_other_versions(
        plan.document_id, keep_version=plan.document_version
    )

    log.info(
        "incremental.persisted",
        document_id=str(plan.document_id),
        document_version=plan.document_version,
        reused=len(plan.reused_rows),
        added=len(plan.added_rows),
        deleted_retry_rows=deleted,
        collected=collected,
    )
    return ChunkSetResult(
        reused=len(plan.reused_rows),
        added=len(plan.added_rows),
        deleted_retry_rows=deleted,
        collected=collected,
    )
