"""Incremental embedding: the FR-ING-03 diff and the R-36 swap (T-205).

The diff half is pure and runs anywhere; the persistence half needs the transactional
`session` fixture, which skips when Postgres is unreachable. Embeddings come from
`FakeEmbeddingClient`, whose determinism is what lets a test assert that an unchanged
document cost zero API inputs — the entire claim of FR-ING-03.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.repositories.chunks import DocumentChunkRepository, FingerprintSource
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import PgVectorRetriever
from app.db.repositories.users import UserRepository
from app.ingestion.chunker import chunk_document_sync
from app.ingestion.incremental import (
    ChunkCarryForwardError,
    diff_chunks,
    persist_chunk_set,
    plan_chunk_set,
)
from app.ingestion.parsers.base import ParsedBlock, ParsedDocument, page_locator
from app.rag.retrieval import RetrievalFilter
from app.services.embeddings import FakeEmbeddingClient

# One paragraph per page, each comfortably under the chunk target, so a "page" maps to
# exactly one chunk and the diff arithmetic in these tests stays legible.
_PAGES = [
    "Corpus ingests documents and answers questions strictly from their contents.",
    "Retrieval is hybrid: dense cosine similarity fused with lexical BM25 scoring.",
    "Every answer carries chunk-level citations the reader can verify for themselves.",
]


def _parsed(pages: list[str]) -> ParsedDocument:
    return ParsedDocument(
        suffix=".pdf",
        blocks=tuple(
            ParsedBlock(text=text, locator=page_locator(number), order=number - 1)
            for number, text in enumerate(pages, start=1)
        ),
        page_count=len(pages),
    )


def _chunked(pages: list[str], *, embedding_model: str = "text-embedding-3-large"):
    return chunk_document_sync(_parsed(pages), embedding_model=embedding_model)


# --- the pure diff ------------------------------------------------------------


def _sources(*fingerprints: str) -> list[FingerprintSource]:
    return [FingerprintSource(embedding_fingerprint=fp, document_version=1) for fp in fingerprints]


def test_nothing_stored_means_everything_is_added() -> None:
    chunked = _chunked(_PAGES)
    diff = diff_chunks(chunked.chunks, [])
    assert len(diff.added) == len(chunked.chunks)
    assert diff.reused == ()


def test_an_unchanged_document_reuses_every_chunk() -> None:
    chunked = _chunked(_PAGES)
    diff = diff_chunks(chunked.chunks, _sources(*(c.embedding_fingerprint for c in chunked.chunks)))
    assert diff.added == ()
    assert diff.total == len(chunked.chunks)


def test_editing_one_page_re_embeds_only_that_page() -> None:
    """The FR-ING-03 headline claim, at the diff level."""
    before = _chunked(_PAGES)
    after = _chunked([_PAGES[0], _PAGES[1] + " Reranking then reorders the candidates.", _PAGES[2]])
    diff = diff_chunks(after.chunks, _sources(*(c.embedding_fingerprint for c in before.chunks)))
    assert len(diff.added) == 1
    assert "Reranking" in diff.added[0].text


def test_inserting_a_page_shifts_every_index_but_re_embeds_only_the_new_text() -> None:
    """`chunk_hash` is text-only precisely so a positional shift is free (R-35(8))."""
    before = _chunked(_PAGES)
    after = _chunked(["A newly prepended cover page for the handbook.", *_PAGES])
    diff = diff_chunks(after.chunks, _sources(*(c.embedding_fingerprint for c in before.chunks)))
    assert len(diff.added) == 1
    assert [chunk.chunk_index for chunk in after.chunks] == [0, 1, 2, 3]
    assert [chunk.chunk_index for chunk in diff.added] == [0]


def test_a_model_change_re_embeds_everything_though_no_text_changed() -> None:
    """R-35(10)'s entire reason for existing: a hash-keyed diff would re-embed nothing."""
    before = _chunked(_PAGES, embedding_model="text-embedding-3-large")
    after = _chunked(_PAGES, embedding_model="text-embedding-3-small")
    assert [c.chunk_hash for c in before.chunks] == [c.chunk_hash for c in after.chunks]
    diff = diff_chunks(after.chunks, _sources(*(c.embedding_fingerprint for c in before.chunks)))
    assert diff.reused == ()
    assert len(diff.added) == len(after.chunks)


def test_repeated_text_reuses_one_source_for_every_copy() -> None:
    """R-35(9) trap A: a consuming match would re-embed occurrences 2..N."""
    boilerplate = "This page intentionally left blank."
    chunked = _chunked([boilerplate, boilerplate, boilerplate])
    fingerprints = {chunk.embedding_fingerprint for chunk in chunked.chunks}
    assert len(fingerprints) == 1  # the trap only exists because hashes collide
    diff = diff_chunks(chunked.chunks, _sources(*fingerprints))
    assert len(diff.reused) == 3
    assert diff.added == ()


def test_obsolete_fingerprints_are_counted() -> None:
    before = _chunked(_PAGES)
    after = _chunked(_PAGES[:1])
    diff = diff_chunks(after.chunks, _sources(*(c.embedding_fingerprint for c in before.chunks)))
    assert diff.obsolete_fingerprints == 2


# --- persistence (DB-backed) --------------------------------------------------


async def _document(session: AsyncSession) -> Document:
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )
    kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
    return await DocumentRepository(session).add(
        Document(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="handbook.pdf",
            storage_uri="s3://corpus/handbook.pdf",
            checksum_sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.ACTIVE,
            searchable=True,
        )
    )


async def _ingest(session: AsyncSession, doc: Document, chunked, version: int, client=None):
    client = client or FakeEmbeddingClient()
    plan = await plan_chunk_set(
        session=session,
        client=client,
        chunked=chunked,
        document_id=doc.id,
        document_version=version,
        knowledge_base_id=doc.knowledge_base_id,
    )
    result = await persist_chunk_set(session, plan=plan)
    return plan, result, client


async def test_first_ingest_writes_a_complete_set_with_no_null_embeddings(
    session: AsyncSession,
) -> None:
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    plan, result, client = await _ingest(session, doc, chunked, 1)

    rows = await DocumentChunkRepository(session).list_by_version(doc.id, 1)
    assert [row.chunk_index for row in rows] == [0, 1, 2]
    assert all(row.embedding is not None for row in rows)
    assert result.total == len(chunked.chunks) == plan.total
    assert client.embedded_inputs == 3


async def test_re_ingesting_unchanged_content_costs_zero_api_inputs(
    session: AsyncSession,
) -> None:
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    await _ingest(session, doc, chunked, 1)

    client = FakeEmbeddingClient()
    plan, result, _ = await _ingest(session, doc, chunked, 2, client)
    assert client.embedded_inputs == 0
    assert result.reused == 3
    assert result.added == 0
    assert plan.total == 3


async def test_a_reused_chunk_keeps_the_exact_vector_it_had(session: AsyncSession) -> None:
    """The assertion that proves R-36(2) carried the vector rather than re-embedding it."""
    doc = await _document(session)
    await _ingest(session, doc, _chunked(_PAGES), 1)
    repo = DocumentChunkRepository(session)
    before = {row.chunk_hash: list(row.embedding) for row in await repo.list_by_version(doc.id, 1)}

    edited = [_PAGES[0], "An entirely different second page about evaluation.", _PAGES[2]]
    await _ingest(session, doc, _chunked(edited), 2)

    after = {row.chunk_hash: list(row.embedding) for row in await repo.list_by_version(doc.id, 2)}
    carried = set(before) & set(after)
    assert len(carried) == 2
    for chunk_hash in carried:
        assert after[chunk_hash] == before[chunk_hash]


async def test_metadata_comes_from_the_new_chunk_not_the_carried_row(
    session: AsyncSession,
) -> None:
    """A fingerprint pins text + model + versions — not the locator.

    Identical text that moved to another page must keep its vector and record its *new*
    page, or FR-CIT-03 sends the reader to the wrong page of their own document.
    """
    doc = await _document(session)
    moving = "A clause that survives the revision word for word."
    await _ingest(session, doc, _chunked([moving, _PAGES[1]]), 1)

    client = FakeEmbeddingClient()
    await _ingest(session, doc, _chunked([_PAGES[1], moving]), 2, client)

    rows = await DocumentChunkRepository(session).list_by_version(doc.id, 2)
    moved = next(row for row in rows if row.chunk_text == moving)
    assert moved.meta["locator"]["page"] == 2
    assert moved.meta["locator"]["label"] == "p. 2"
    assert client.embedded_inputs == 0  # both texts were already stored


async def test_duplicate_source_fingerprints_do_not_fan_out_the_insert(
    session: AsyncSession,
) -> None:
    """R-35(9) trap B: without DISTINCT ON this trips the unique constraint."""
    doc = await _document(session)
    boilerplate = "This page intentionally left blank."
    chunked = _chunked([boilerplate, boilerplate, boilerplate])
    _, first, client = await _ingest(session, doc, chunked, 1)
    assert client.embedded_inputs == 1  # deduped calls...
    assert first.total == 3  # ...but never deduped rows

    _, second, client2 = await _ingest(session, doc, chunked, 2)
    assert client2.embedded_inputs == 0
    rows = await DocumentChunkRepository(session).list_by_version(doc.id, 2)
    assert [row.chunk_index for row in rows] == [0, 1, 2]
    assert second.reused == 3


async def test_a_null_embedding_source_is_re_embedded_not_carried(
    session: AsyncSession,
) -> None:
    """R-36's quietest failure: a fingerprint match with no vector behind it.

    Carrying it forward would leave a row `PgVectorRetriever` filters out — a passage the
    user uploaded, unfindable forever, on a document reporting ACTIVE.
    """
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    await _ingest(session, doc, chunked, 1)

    repo = DocumentChunkRepository(session)
    victim = (await repo.list_by_version(doc.id, 1))[1]
    victim.embedding = None
    await session.flush()

    client = FakeEmbeddingClient()
    _, result, _ = await _ingest(session, doc, chunked, 2, client)
    assert client.embedded_inputs == 1
    assert result.added == 1
    assert result.reused == 2
    assert await repo.count_missing_embeddings(doc.id, 2) == 0


async def test_the_swap_leaves_only_the_new_version(session: AsyncSession) -> None:
    """R-36(4), performed inside the swap so no window exists in which both are live."""
    doc = await _document(session)
    await _ingest(session, doc, _chunked(_PAGES), 1)
    _, result, _ = await _ingest(session, doc, _chunked(_PAGES), 2)

    repo = DocumentChunkRepository(session)
    assert await repo.list_by_version(doc.id, 1) == []
    assert len(await repo.list_by_version(doc.id, 2)) == 3
    assert result.collected == 3


async def test_retrieval_never_sees_two_versions_of_a_chunk(session: AsyncSession) -> None:
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    await _ingest(session, doc, chunked, 1)
    await _ingest(session, doc, chunked, 2)
    # T-207 moves this pointer in the same transaction as the swap. T-206's retriever
    # filters `document_version = documents.current_version` (R-37(9)), so a swap that
    # writes v2 rows without advancing the pointer makes the document unretrievable —
    # which is precisely the coupling this line stands in for until T-207 exists.
    doc.current_version = 2
    await session.flush()

    client = FakeEmbeddingClient()
    query = await client.embed_query(_PAGES[0])
    hits = await PgVectorRetriever(session).search(
        _PAGES[0],
        query,
        filters=RetrievalFilter(
            owner_id=doc.owner_id, tenant_id=DEFAULT_TENANT_ID, document_ids=[doc.id]
        ),
    )
    assert len(hits) == 3
    assert len({hit.chunk_index for hit in hits}) == 3


async def test_re_running_the_same_version_is_idempotent(session: AsyncSession) -> None:
    """FR-ING-04: a retry deletes the in-progress version and rebuilds (R-36(5))."""
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    await _ingest(session, doc, chunked, 1)
    _, second, _ = await _ingest(session, doc, chunked, 1)

    assert second.deleted_retry_rows == 3
    assert second.total == 3
    rows = await DocumentChunkRepository(session).list_by_version(doc.id, 1)
    assert [row.chunk_index for row in rows] == [0, 1, 2]


async def test_a_vanished_source_vector_fails_loudly_and_retryably(
    session: AsyncSession,
) -> None:
    """An omitted row would be silent; the rowcount check makes it a retryable failure."""
    doc = await _document(session)
    chunked = _chunked(_PAGES)
    await _ingest(session, doc, chunked, 1)

    plan = await plan_chunk_set(
        session=session,
        client=FakeEmbeddingClient(),
        chunked=chunked,
        document_id=doc.id,
        document_version=2,
        knowledge_base_id=doc.knowledge_base_id,
    )
    assert len(plan.reused_rows) == 3
    # The source disappears between the plan and the write.
    await DocumentChunkRepository(session).delete_by_version(doc.id, 1)

    with pytest.raises(ChunkCarryForwardError) as excinfo:
        await persist_chunk_set(session, plan=plan)
    assert excinfo.value.retryable is True
    assert excinfo.value.code == "CHUNK_CARRY_INCOMPLETE"


async def test_fingerprint_sources_excludes_the_version_being_written(
    session: AsyncSession,
) -> None:
    doc = await _document(session)
    await _ingest(session, doc, _chunked(_PAGES), 1)
    repo = DocumentChunkRepository(session)
    assert repo.model is DocumentChunk
    assert await repo.fingerprint_sources(doc.id, exclude_version=1) == []
    assert len(await repo.fingerprint_sources(doc.id, exclude_version=2)) == 3


# --- placement (R-31: ingest in the worker, never in the API process) ---------


def test_no_api_module_imports_the_incremental_pipeline() -> None:
    """`app.services.embeddings` is deliberately exempt — T-206 needs `embed_query`."""
    api_dir = Path(__file__).resolve().parents[1] / "app" / "api"
    offenders = [
        path.name
        for path in api_dir.glob("*.py")
        if "ingestion.incremental" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} import the incremental pipeline into the API process — R-31 (§8.12) "
        "requires ingestion to happen in the worker"
    )
