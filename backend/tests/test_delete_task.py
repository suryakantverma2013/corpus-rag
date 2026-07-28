"""The deletion task end to end (T-208, FR-ING-05, R-39).

Same harness as `test_ingest_task.py` — one connection inside a rolled-back outer
transaction, so the task's real `commit()` calls become savepoint releases — because the
questions worth asking here are also about *what landed in which transaction*.

Two of these tests are the reason the file exists rather than a couple of assertions bolted
onto the API suite:

* `test_a_dead_lettered_purge_never_marks_the_document_failed` pins R-39(7). `FAILED` renders
  as `Failed` in FR-KBM-04 and pairs with a Retry that would re-*ingest* the document the
  user deleted.
* `test_the_objects_are_gone_before_the_row_says_deleted` pins R-39(9)'s ordering by
  crashing between the two — the state a real worker leaves behind when it dies mid-purge.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from arq.worker import Retry
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, WorkerSettings, get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.jobs import DOCUMENT_DELETED
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.services.embeddings import FakeEmbeddingClient
from app.services.object_storage import (
    LocalFilesystemStorage,
    ObjectStorageError,
    original_key,
)
from workers.delete import delete_document

_TEXT = "Corpus answers strictly from document contents, with chunk-level citations."


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(get_settings().database.url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, DBAPIError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable for deletion tests: {exc}")

    txn = await conn.begin()
    maker = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield maker
    finally:
        if txn.is_active:
            await txn.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "objects")


class _BrokenStorage:
    """Storage whose prefix purge always fails — the retryable failure mode."""

    def __init__(self) -> None:
        self.calls = 0

    async def delete_prefix(self, prefix: str) -> int:
        self.calls += 1
        raise ObjectStorageError("bucket unreachable")


async def _fixtures(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFilesystemStorage,
    *,
    status: DocumentStatus = DocumentStatus.DELETE_PENDING,
    chunks: int = 3,
    versions: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """User + KB + document + chunk rows + stored originals + a QUEUED delete job."""
    document_id = uuid.uuid4()
    embedder = FakeEmbeddingClient()
    async with sessions() as session:
        user = await UserRepository(session).upsert_from_claims(
            sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
        )
        kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)

        uri = None
        for version in range(1, versions + 1):
            key = original_key(
                tenant_id=DEFAULT_TENANT_ID,
                knowledge_base_id=kb.id,
                document_id=document_id,
                version=version,
                filename="handbook.pdf",
            )
            stored = await storage.put(key, b"%PDF-1.7\n%%EOF\n")
            uri = stored.uri

        document = Document(
            id=document_id,
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="handbook.pdf",
            storage_uri=uri or "",
            checksum_sha256=uuid.uuid4().hex * 2,
            size_bytes=16,
            status=status,
            current_version=versions,
            searchable=False,
            chunk_count=chunks * versions,
        )
        session.add(document)
        await session.flush()

        for version in range(1, versions + 1):
            for index in range(chunks):
                session.add(
                    DocumentChunk(
                        document_id=document_id,
                        document_version=version,
                        chunk_index=index,
                        chunk_hash=uuid.uuid4().hex * 2,
                        embedding_fingerprint=uuid.uuid4().hex * 2,
                        token_count=20,
                        tenant_id=DEFAULT_TENANT_ID,
                        knowledge_base_id=kb.id,
                        chunk_text=f"{_TEXT} ({version}.{index})",
                        embedding=await embedder.embed_query(f"{_TEXT} ({version}.{index})"),
                        meta={"block_order": 0, "block_chunk_index": index},
                    )
                )

        job = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.DELETE,
            status=JobStatus.QUEUED,
            document_version=versions,
            idempotency_key=f"delete:{document_id}",
        )
        session.add(job)
        await session.commit()
        return document_id, job.id, user.id


def _ctx(
    sessions: async_sessionmaker[AsyncSession],
    storage: object,
    *,
    job_try: int = 1,
    max_tries: int = 5,
) -> dict[str, object]:
    return {
        "settings": Settings(worker=WorkerSettings(max_tries=max_tries, retry_base_seconds=0.01)),
        "sessionmaker": sessions,
        "storage": storage,
        "job_try": job_try,
    }


async def _reload(
    sessions: async_sessionmaker[AsyncSession], document_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[Document, KnowledgeJob]:
    async with sessions() as session:
        document = await session.get(Document, document_id)
        job = await session.get(KnowledgeJob, job_id)
        assert document is not None and job is not None
        await session.refresh(document)
        await session.refresh(job)
        return document, job


async def _chunk_count(sessions: async_sessionmaker[AsyncSession], document_id: uuid.UUID) -> int:
    async with sessions() as session:
        stmt = select(DocumentChunk).where(DocumentChunk.document_id == document_id)
        return len(list((await session.scalars(stmt)).all()))


def _object_count(storage: LocalFilesystemStorage) -> int:
    import os

    if not os.path.isdir(storage.root):
        return 0
    return sum(len(files) for _, _, files in os.walk(storage.root))


# --- the happy path -----------------------------------------------------------------


async def test_a_document_is_purged_and_marked_deleted(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    assert _object_count(storage) == 1

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.DELETED
    assert document.deleted_at is not None
    assert document.searchable is False
    assert document.chunk_count == 0
    assert await _chunk_count(sessions, document_id) == 0
    assert _object_count(storage) == 0
    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == 100
    assert job.attempt_count == 1
    assert job.completed_at is not None


async def test_every_version_is_purged_not_only_the_current_one(sessions, storage) -> None:  # noqa: ANN001
    """The *document* prefix, unlike T-207's malware purge which spares older versions."""
    document_id, job_id, _ = await _fixtures(sessions, storage, chunks=2, versions=3)
    assert _object_count(storage) == 3

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    assert await _chunk_count(sessions, document_id) == 0
    assert _object_count(storage) == 0


async def test_the_row_survives_so_the_deletion_is_recorded(sessions, storage) -> None:  # noqa: ANN001
    """FR-ING-05 asks for a `DELETED` mark with a timestamp — not for the row to vanish."""
    document_id, job_id, _ = await _fixtures(sessions, storage)

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    async with sessions() as session:
        assert await session.get(Document, document_id) is not None


# --- FR-ING-04 idempotency ----------------------------------------------------------


async def test_an_already_deleted_document_short_circuits(sessions, storage) -> None:  # noqa: ANN001
    """FR-ING-04 names this case explicitly."""
    document_id, job_id, _ = await _fixtures(
        sessions, storage, status=DocumentStatus.DELETED, chunks=0
    )

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    _, job = await _reload(sessions, document_id, job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempt_count == 0, "the short-circuit must not consume an attempt"
    assert _object_count(storage) == 1, "nothing was purged for an already-deleted document"


async def test_an_already_succeeded_job_is_a_no_op(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
        job.status = JobStatus.SUCCEEDED
        await session.commit()

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    document, _ = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.DELETE_PENDING
    assert _object_count(storage) == 1


async def test_replaying_the_task_is_safe(sessions, storage) -> None:  # noqa: ANN001
    """A crashed worker's redelivery: the second run finds nothing left and still succeeds."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage)

    await delete_document(ctx, str(document_id), str(job_id))
    # Force the job back to QUEUED — what a crash *before* the SUCCEEDED write looks like.
    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
        job.status = JobStatus.QUEUED
        await session.commit()

    await delete_document(ctx, str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.DELETED
    assert job.status is JobStatus.SUCCEEDED
    assert await _chunk_count(sessions, document_id) == 0


async def test_a_missing_row_is_not_an_error(sessions, storage) -> None:  # noqa: ANN001
    await delete_document(_ctx(sessions, storage), str(uuid.uuid4()), str(uuid.uuid4()))


# --- failure handling (R-39(7)) ------------------------------------------------------


async def test_a_storage_failure_retries_with_backoff(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    broken = _BrokenStorage()

    with pytest.raises(Retry):
        await delete_document(_ctx(sessions, broken, job_try=1), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert job.status is JobStatus.QUEUED
    assert job.error_code == "OBJECT_STORAGE_UNAVAILABLE"
    assert job.attempt_count == 1
    # R-39(7): the document is in the deletion path, and stays there.
    assert document.status is DocumentStatus.DELETING
    assert await _chunk_count(sessions, document_id) == 3, "nothing was dropped before the purge"


async def test_a_dead_lettered_purge_never_marks_the_document_failed(sessions, storage) -> None:  # noqa: ANN001
    """R-39(7). `FAILED` would render as `Failed` and offer a Retry that re-ingests."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    broken = _BrokenStorage()

    # The final attempt: arq would not deliver this job again, so the task must write the
    # dead-letter itself and return rather than raise (R-38(4)).
    await delete_document(
        _ctx(sessions, broken, job_try=3, max_tries=3), str(document_id), str(job_id)
    )

    document, job = await _reload(sessions, document_id, job_id)
    assert job.status is JobStatus.DEAD_LETTER
    assert job.error_code == "OBJECT_STORAGE_UNAVAILABLE"
    assert "dead-lettered after 3 attempts" in (job.error_message or "")
    assert document.status is DocumentStatus.DELETING
    assert document.status is not DocumentStatus.FAILED
    assert document.searchable is False, "still out of retrieval, which is what matters"
    assert document.deleted_at is None


async def test_a_dead_lettered_purge_is_re_drivable(sessions, storage) -> None:  # noqa: ANN001
    """The other half of R-39(7): stuck is recoverable, via a repeat DELETE."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    await delete_document(
        _ctx(sessions, _BrokenStorage(), job_try=3, max_tries=3), str(document_id), str(job_id)
    )

    # What `request_deletion` does on the second DELETE.
    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
        job.status = JobStatus.QUEUED
        job.error_code = None
        await session.commit()

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.DELETED
    assert job.status is JobStatus.SUCCEEDED
    assert _object_count(storage) == 0


# --- ordering (R-39(9)) --------------------------------------------------------------


async def test_the_objects_are_gone_before_the_row_says_deleted(sessions, storage) -> None:  # noqa: ANN001
    """R-39(9): a crash between the purge and the commit must leave no orphaned bytes.

    Simulated by a storage backend that purges for real and then makes the following
    transaction fail. The document must not be `DELETED` — and the objects must already be
    gone, which is the ordering that makes the retry a no-op rather than a leak.
    """

    class _PurgeThenBreak:
        def __init__(self, inner: LocalFilesystemStorage) -> None:
            self.inner = inner

        async def delete_prefix(self, prefix: str) -> int:
            removed = await self.inner.delete_prefix(prefix)
            raise ObjectStorageError(f"connection lost after removing {removed}")

    document_id, job_id, _ = await _fixtures(sessions, storage)
    assert _object_count(storage) == 1

    with pytest.raises(Retry):
        await delete_document(
            _ctx(sessions, _PurgeThenBreak(storage)), str(document_id), str(job_id)
        )

    document, _ = await _reload(sessions, document_id, job_id)
    assert _object_count(storage) == 0, "objects were purged first"
    assert document.status is DocumentStatus.DELETING, "the row does not claim DELETED yet"
    assert document.deleted_at is None

    # And the retry completes cleanly against a bucket that is already empty.
    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))
    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.DELETED
    assert job.status is JobStatus.SUCCEEDED


async def test_an_ingest_job_enqueued_in_the_gap_is_superseded(sessions, storage) -> None:  # noqa: ANN001
    """The last of R-39(8)'s three points, closed inside the terminal transaction."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    async with sessions() as session:
        session.add(
            KnowledgeJob(
                document_id=document_id,
                job_type=JobType.INGEST,
                status=JobStatus.QUEUED,
                document_version=1,
                idempotency_key=f"ingest:{document_id}:v1",
            )
        )
        await session.commit()

    await delete_document(_ctx(sessions, storage), str(document_id), str(job_id))

    async with sessions() as session:
        stmt = select(KnowledgeJob).where(
            KnowledgeJob.document_id == document_id, KnowledgeJob.job_type == JobType.INGEST
        )
        ingest = (await session.scalars(stmt)).one()
    assert ingest.status is JobStatus.FAILED
    assert ingest.error_code == DOCUMENT_DELETED
