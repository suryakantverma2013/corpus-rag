"""The ingestion task end to end (T-207, FR-ING-01/03/04, R-36/R-37).

DB-backed, and deliberately so: almost everything worth asserting here is about *what
landed in which transaction*. Skips when Postgres is unreachable, like every other
repository suite.

The sessionmaker is bound to a single connection inside an outer transaction that is
rolled back on teardown, so the task's real `commit()` calls become savepoint releases and
the suite leaves nothing behind. Embeddings come from `FakeEmbeddingClient` and objects
from `LocalFilesystemStorage`, so no network is involved; the scanner is a stub, since the
real screens have their own suite.

The single most valuable assertion in this file is
`test_an_ingested_document_is_actually_retrievable`: it is the only place the R-37(9)
contract — retrieval filters `document_version = documents.current_version`, and nothing
but this task advances that pointer — is checked end to end rather than field by field.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pymupdf
import pytest
from arq.worker import Retry
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import workers.ingest
from app.config import ScannerSettings, Settings, WorkerSettings, get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.chunks import DocumentChunkRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import HybridRetriever
from app.db.repositories.users import UserRepository
from app.ingestion.scanner import REASON_MACRO, ScanResult, ScanVerdict
from app.rag.retrieval import RetrievalFilter
from app.services.clamav import ClamAVUnavailableError
from app.services.embeddings import FakeEmbeddingClient
from app.services.model_selection import ModelSlot, set_model_override
from app.services.object_storage import LocalFilesystemStorage, original_key
from workers.ingest import ingest_document

_TEXT = (
    "Corpus ingests documents and answers questions strictly from their contents. "
    "Retrieval is hybrid, and every answer carries chunk-level citations. "
    "The perihelion precession of Mercury is mentioned here as a rare lexical anchor."
)


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker whose sessions all share one rolled-back connection."""
    engine = create_async_engine(get_settings().database.url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, DBAPIError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable for ingestion tests: {exc}")

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


class _StubScanner:
    def __init__(self, result: ScanResult | None = None, error: Exception | None = None):
        self.result = result or ScanResult.clean()
        self.error = error
        self.calls: list[str] = []

    async def scan(self, payload: bytes, *, filename: str) -> ScanResult:
        self.calls.append(filename)
        if self.error is not None:
            raise self.error
        return self.result


def _pdf(text: str = _TEXT) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 720), text)
    raw = document.tobytes()
    document.close()
    return raw


async def _fixtures(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFilesystemStorage,
    *,
    payload: bytes | None = None,
    filename: str = "handbook.pdf",
    status: DocumentStatus = DocumentStatus.QUEUED,
    current_version: int = 1,
    job_version: int = 1,
    job_status: JobStatus = JobStatus.QUEUED,
    error_code: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create user + KB + document + job and store the original. Returns their ids."""
    document_id = uuid.uuid4()
    async with sessions() as session:
        user = await UserRepository(session).upsert_from_claims(
            sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
        )
        kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
        key = original_key(
            tenant_id=DEFAULT_TENANT_ID,
            knowledge_base_id=kb.id,
            document_id=document_id,
            version=job_version,
            filename=filename,
        )
        stored = await storage.put(key, payload if payload is not None else _pdf())

        document = Document(
            id=document_id,
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename=filename,
            storage_uri=stored.uri,
            checksum_sha256=uuid.uuid4().hex * 2,
            size_bytes=stored.size,
            status=status,
            current_version=current_version,
            searchable=status is DocumentStatus.ACTIVE,
        )
        job = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.INGEST,
            status=job_status,
            document_version=job_version,
            error_code=error_code,
            idempotency_key=f"ingest:{document_id}:v{job_version}",
        )
        session.add_all([document, job])
        await session.commit()
        return document_id, job.id, user.id


def _ctx(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFilesystemStorage,
    scanner: _StubScanner,
    *,
    job_try: int = 1,
    max_tries: int = 5,
) -> dict[str, object]:
    return {
        "settings": Settings(
            worker=WorkerSettings(max_tries=max_tries, retry_base_seconds=0.01),
            scanner=ScannerSettings(backend="structural"),
        ),
        "sessionmaker": sessions,
        "storage": storage,
        "embedder": FakeEmbeddingClient(),
        "scanner": scanner,
        "job_try": job_try,
    }


async def _reload(
    sessions: async_sessionmaker[AsyncSession], document_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[Document, KnowledgeJob]:
    async with sessions() as session:
        document = await session.get(Document, document_id)
        job = await session.get(KnowledgeJob, job_id)
        assert document is not None and job is not None
        return document, job


# --- the happy path -----------------------------------------------------------------


async def test_a_document_walks_to_active_in_one_swap(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    scanner = _StubScanner()

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.ACTIVE
    assert document.searchable is True
    assert document.chunk_count and document.chunk_count > 0
    assert document.page_count == 1  # PDF-only, from the parser (R-34)
    assert document.error_message is None
    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == 100
    assert job.attempt_count == 1
    assert job.started_at is not None and job.completed_at is not None
    # The screen ran, and it ran before anything parsed the file (R-31).
    assert scanner.calls == ["handbook.pdf"]

    async with sessions() as session:
        chunks = await DocumentChunkRepository(session).list_by_version(document_id, 1)
    assert len(chunks) == document.chunk_count
    assert all(chunk.embedding is not None for chunk in chunks)


async def test_an_ingested_document_is_actually_retrievable(sessions, storage) -> None:  # noqa: ANN001
    """The R-37(9) contract, end to end.

    Retrieval joins `document_chunks.document_version = documents.current_version`, and
    `persist_chunk_set` deliberately leaves that pointer alone — so if this task failed to
    advance it, every field asserted above would still be correct and the document would
    match nothing. Only a real query catches that.
    """
    document_id, job_id, owner_id = await _fixtures(sessions, storage)
    embedder = FakeEmbeddingClient()
    ctx = _ctx(sessions, storage, _StubScanner())
    ctx["embedder"] = embedder

    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None and document.current_version == 1
        hits = await HybridRetriever(session).search(
            "perihelion precession of Mercury",
            await embedder.embed_query("perihelion precession of Mercury"),
            filters=RetrievalFilter(owner_id=owner_id),
        )

    assert hits, "an ACTIVE document returned nothing — current_version was not advanced"
    assert all(hit.document_id == document_id for hit in hits)


async def test_replaying_the_whole_task_leaves_exactly_one_version(sessions, storage) -> None:  # noqa: ANN001
    """§11 'worker crashes after vector insertion' — a redelivery must not duplicate."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())

    await ingest_document(ctx, str(document_id), str(job_id))
    first, _ = await _reload(sessions, document_id, job_id)

    # A redelivery of the same job. The FR-ING-04 short-circuit catches it, but re-running
    # from scratch (below) must also be safe — that is what a crash *before* SUCCEEDED
    # looks like.
    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
        job.status = JobStatus.QUEUED
        await session.commit()
    await ingest_document(ctx, str(document_id), str(job_id))

    document, _ = await _reload(sessions, document_id, job_id)
    async with sessions() as session:
        chunks = await DocumentChunkRepository(session).list_by_document(document_id)
    assert document.chunk_count == first.chunk_count
    assert len(chunks) == first.chunk_count


# --- FR-ING-04 short-circuits -------------------------------------------------------


async def test_an_already_succeeded_job_is_a_no_op(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(
        sessions, storage, job_status=JobStatus.SUCCEEDED, status=DocumentStatus.ACTIVE
    )
    scanner = _StubScanner()

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    assert scanner.calls == []  # nothing was even fetched


async def test_an_active_document_at_this_version_short_circuits(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(
        sessions, storage, status=DocumentStatus.ACTIVE, current_version=1, job_version=1
    )
    scanner = _StubScanner()

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    _, job = await _reload(sessions, document_id, job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert scanner.calls == []


async def test_a_stale_job_cannot_roll_a_replaced_document_backwards(sessions, storage) -> None:  # noqa: ANN001
    """`>=`, not `==`: a redelivered v1 job must not rebuild v1 over a live v2."""
    document_id, job_id, _ = await _fixtures(
        sessions, storage, status=DocumentStatus.ACTIVE, current_version=2, job_version=1
    )
    scanner = _StubScanner()

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.current_version == 2
    assert job.status is JobStatus.SUCCEEDED
    assert scanner.calls == []


@pytest.mark.parametrize(
    "state", [DocumentStatus.DELETE_PENDING, DocumentStatus.DELETING, DocumentStatus.DELETED]
)
async def test_a_document_being_deleted_is_never_resurrected(sessions, storage, state) -> None:  # noqa: ANN001
    """§11 'delete while ingestion is running'. FR-ING-05 already made it non-searchable."""
    document_id, job_id, _ = await _fixtures(sessions, storage, status=state)
    scanner = _StubScanner()

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is state  # untouched — the deletion flow still owns it
    assert job.status is JobStatus.FAILED
    assert job.error_code == "DOCUMENT_DELETED"
    assert scanner.calls == []


async def test_a_deletion_landing_mid_ingest_aborts_the_swap(sessions, storage) -> None:  # noqa: ANN001
    """R-39(8)'s third point — the one `_begin`'s short-circuit cannot cover.

    `_begin` ran before the parse, the scan and the embedding calls; a `DELETE` arriving
    after it is invisible there. Without the `FOR UPDATE` re-read in front of `mark_active`,
    this swap commits `searchable = True` and a version bump over a deletion in progress —
    resurrecting a document whose bytes the purge may already have removed.

    The delete is injected by patching `_plan_on_a_disposable_session` — the last thing to
    run before the swap — so it lands in exactly the window the guard exists to close. It
    writes what `request_deletion` writes, **including the job row**, because the task's own
    `set_status(INDEXING)` overwrites the status field from its stale ORM instance. That
    clobber is why the guard cannot rely on `documents.status` alone.

    (Patched at this level rather than at `plan_chunk_set`: that runs *inside* the disposable
    session, and under this fixture's savepoint nesting the injected write would be rolled
    back when that session closes.)
    """
    document_id, job_id, owner_id = await _fixtures(sessions, storage)
    real_plan = workers.ingest._plan_on_a_disposable_session

    async def _plan_then_delete(**kwargs):  # noqa: ANN003, ANN202
        plan = await real_plan(**kwargs)
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            document.status = DocumentStatus.DELETE_PENDING
            document.searchable = False
            session.add(
                KnowledgeJob(
                    document_id=document_id,
                    job_type=JobType.DELETE,
                    status=JobStatus.QUEUED,
                    document_version=1,
                    idempotency_key=f"delete:{document_id}",
                )
            )
            await session.commit()
        return plan

    workers.ingest._plan_on_a_disposable_session = _plan_then_delete
    try:
        await ingest_document(
            _ctx(sessions, storage, _StubScanner()), str(document_id), str(job_id)
        )
    finally:
        workers.ingest._plan_on_a_disposable_session = real_plan

    document, job = await _reload(sessions, document_id, job_id)
    # `searchable` is the one that matters: FR-ING-05 promises the document has left
    # retrieval, and `mark_active` is the only thing that could put it back.
    assert document.searchable is False, "the swap resurrected the document"
    assert document.status is not DocumentStatus.ACTIVE
    assert document.current_version == 1
    # Terminal, and recorded on the job — never on the document (R-39(7)).
    assert job.status is JobStatus.FAILED
    assert job.error_code == "DOCUMENT_DELETED"

    async with sessions() as session:
        chunks = await DocumentChunkRepository(session).list_by_document(document_id)
    assert chunks == [], "the aborted swap must leave no chunk rows behind"


# --- failure classification ---------------------------------------------------------


async def test_a_parse_failure_is_terminal_and_does_not_retry(sessions, storage) -> None:  # noqa: ANN001
    """R-34: no parse failure becomes parseable on a retry, so no backoff is scheduled."""
    document_id, job_id, _ = await _fixtures(
        sessions, storage, payload=b"%PDF-1.7\nnot really a pdf"
    )

    # No `pytest.raises` — a terminal failure must return normally.
    await ingest_document(_ctx(sessions, storage, _StubScanner()), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert job.error_code == "CORRUPT_DOCUMENT"
    assert document.error_message


async def test_a_retryable_failure_raises_retry_and_leaves_the_document_in_flight(
    sessions,  # noqa: ANN001
    storage,  # noqa: ANN001
) -> None:
    """R-38(5): FAILED means *terminally* failed; retries live on the job row.

    Flapping the document into FAILED and back would churn the FR-KBM-04 badge and the
    T-210 SSE stream once per attempt, for a state the user cannot act on.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    scanner = _StubScanner(error=ClamAVUnavailableError("connection refused"))

    with pytest.raises(Retry):
        await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.QUEUED  # still pre-PARSING, not FAILED
    assert document.error_message is None
    assert job.status is JobStatus.QUEUED
    assert job.error_code == "SCANNER_UNAVAILABLE"
    assert job.attempt_count == 1


async def test_the_final_attempt_dead_letters_instead_of_raising(sessions, storage) -> None:  # noqa: ANN001
    """arq finishes an exhausted job without invoking the task, so it must self-detect.

    Were this to raise `Retry` on the last attempt, arq's own `max_tries` check would
    discard the next delivery before this code ever ran and the job would sit `RUNNING`
    forever.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    scanner = _StubScanner(error=ClamAVUnavailableError("connection refused"))
    ctx = _ctx(sessions, storage, scanner, job_try=3, max_tries=3)

    await ingest_document(ctx, str(document_id), str(job_id))  # must NOT raise

    document, job = await _reload(sessions, document_id, job_id)
    assert job.status is JobStatus.DEAD_LETTER
    assert job.error_code == "SCANNER_UNAVAILABLE"
    assert document.status is DocumentStatus.FAILED  # now terminal, so the badge is right
    assert "dead-lettered" in (job.error_message or "")


async def test_a_missing_original_is_terminal(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        await storage.delete(storage.key_for_uri(document.storage_uri))

    await ingest_document(_ctx(sessions, storage, _StubScanner()), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.FAILED
    assert job.error_code == "OBJECT_MISSING"
    assert job.status is JobStatus.FAILED  # terminal, not dead-lettered


# --- malware (R-31(1)) --------------------------------------------------------------


async def test_a_detection_fails_the_document_and_purges_the_object(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, owner_id = await _fixtures(sessions, storage)
    scanner = _StubScanner(
        ScanResult(
            verdict=ScanVerdict.INFECTED,
            reason_code=REASON_MACRO,
            detail="container holds 'word/vbaProject.bin'",
        )
    )

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    document, job = await _reload(sessions, document_id, job_id)
    assert document.status is DocumentStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert job.error_code == REASON_MACRO
    assert "vbaProject.bin" in (document.error_message or "")

    # The bytes are gone, but the row survives so the KB surface can say why (R-31(1):
    # a failed ingestion, not an FR-ING-05 deletion).
    assert await storage.exists(storage.key_for_uri(document.storage_uri)) is False

    async with sessions() as session:
        events = await AuditLogRepository(session).list_events(actor_id=owner_id)
    # `audit_logs.target_id` is a string column, not a UUID one.
    assert [(event.target_id, event.details) for event in events] == [
        (str(document_id), {"action": "malware_purge"})
    ]


async def test_a_detection_leaves_no_chunks_behind(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    scanner = _StubScanner(
        ScanResult(verdict=ScanVerdict.INFECTED, reason_code=REASON_MACRO, detail="macro")
    )

    await ingest_document(_ctx(sessions, storage, scanner), str(document_id), str(job_id))

    async with sessions() as session:
        chunks = await DocumentChunkRepository(session).list_by_document(document_id)
    assert chunks == []


# --- replace: the version swap and the superseded purge (T-209, R-40(3)/(4)) --------


_V2_TEXT = (
    "Version two of this handbook replaces the first entirely. "
    "The Chandrasekhar limit for white dwarf stars is the anchor unique to this revision."
)


async def _stage_replacement(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFilesystemStorage,
    *,
    document_id: uuid.UUID,
    version: int,
    payload: bytes,
) -> uuid.UUID:
    """Leave behind exactly what `replace_document` leaves behind, and nothing more.

    New original at `v{version}`, `storage_uri` repointed at it, the document back to
    `QUEUED`, a job at the new target — and `current_version`, `searchable` and the old
    chunk rows untouched, which is the whole of R-36(3).
    """
    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        key = original_key(
            tenant_id=document.tenant_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document_id,
            version=version,
            filename=document.filename,
        )
        stored = await storage.put(key, payload)
        document.storage_uri = stored.uri
        document.status = DocumentStatus.QUEUED
        job = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            document_version=version,
            idempotency_key=f"ingest:{document_id}:v{version}",
        )
        session.add(job)
        await session.commit()
        return job.id


def _original_key_for(document: Document, *, version: int) -> str:
    return original_key(
        tenant_id=document.tenant_id,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        version=version,
        filename=document.filename,
    )


async def test_a_replaced_document_answers_from_the_new_version_only(sessions, storage) -> None:  # noqa: ANN001
    """The T-206/T-208 precedent applied to replace: prove it with a real query.

    Every field would look correct with the version pointer left behind or the old rows
    still present — the document would simply serve the wrong text, which no field
    assertion detects.
    """
    document_id, job_id, owner_id = await _fixtures(sessions, storage)
    embedder = FakeEmbeddingClient()
    ctx = _ctx(sessions, storage, _StubScanner())
    ctx["embedder"] = embedder
    await ingest_document(ctx, str(document_id), str(job_id))

    v2_job_id = await _stage_replacement(
        sessions, storage, document_id=document_id, version=2, payload=_pdf(_V2_TEXT)
    )
    await ingest_document(ctx, str(document_id), str(v2_job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status is DocumentStatus.ACTIVE
        assert document.current_version == 2
        assert document.searchable is True

        chunks = DocumentChunkRepository(session)
        assert await chunks.list_by_version(document_id, 1) == []
        assert await chunks.list_by_version(document_id, 2) != []

        hits = await HybridRetriever(session).search(
            "Chandrasekhar limit for white dwarf stars",
            await embedder.embed_query("Chandrasekhar limit for white dwarf stars"),
            filters=RetrievalFilter(owner_id=owner_id),
        )

    assert hits, "the replaced document returned nothing — current_version did not advance"
    served = " ".join(hit.chunk_text for hit in hits)
    assert "Chandrasekhar" in served
    assert "perihelion" not in served, "the superseded version is still being served"


async def test_the_superseded_versions_objects_are_purged_after_the_swap(sessions, storage) -> None:  # noqa: ANN001
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())
    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        v1_key = _original_key_for(document, version=1)
        v2_key = _original_key_for(document, version=2)
    assert await storage.exists(v1_key)

    v2_job_id = await _stage_replacement(
        sessions, storage, document_id=document_id, version=2, payload=_pdf(_V2_TEXT)
    )
    await ingest_document(ctx, str(document_id), str(v2_job_id))

    assert await storage.exists(v1_key) is False
    assert await storage.exists(v2_key) is True


async def test_a_same_version_retry_does_not_purge_its_own_original(sessions, storage) -> None:  # noqa: ANN001
    """The `range(first, last)` guard.

    On a first ingest and on every FR-ING-04 same-version retry `previous_version ==
    target_version`. An unguarded purge would delete the original of the document that has
    just gone `ACTIVE` — invisibly, until a later retry fails `OBJECT_MISSING` on a
    document nothing can rebuild.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())
    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        v1_key = _original_key_for(document, version=1)
        # A fresh same-version job, exactly as `retry_ingestion` mints one.
        document.status = DocumentStatus.QUEUED
        retry = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            document_version=1,
            idempotency_key=f"ingest:{document_id}:v1:r1",
        )
        session.add(retry)
        await session.commit()
        retry_id = retry.id

    await ingest_document(ctx, str(document_id), str(retry_id))

    assert await storage.exists(v1_key) is True
    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None and document.status is DocumentStatus.ACTIVE


async def test_versions_skipped_by_a_burned_replace_are_also_purged(sessions, storage) -> None:  # noqa: ANN001
    """A replace whose ingestion failed has already consumed v2; its original is referenced
    by nothing once `storage_uri` moves on to v3."""
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())
    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        v1_key = _original_key_for(document, version=1)
        v2_key = _original_key_for(document, version=2)
        v3_key = _original_key_for(document, version=3)
        # The failed replace's original, which nothing now points at.
        await storage.put(v2_key, _pdf(_V2_TEXT))

    v3_job_id = await _stage_replacement(
        sessions, storage, document_id=document_id, version=3, payload=_pdf(_V2_TEXT)
    )
    await ingest_document(ctx, str(document_id), str(v3_job_id))

    assert await storage.exists(v1_key) is False
    assert await storage.exists(v2_key) is False
    assert await storage.exists(v3_key) is True


async def test_a_failed_swap_leaves_the_old_versions_objects_intact(  # noqa: ANN001
    sessions, storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The R-40(3) ordering proof, and the counterpart of the deletion task's
    crash-between-purge-and-commit test: until the swap commits, v(n) is still the
    document's live content and its original must survive."""
    from app.db.repositories.documents import DocumentRepository

    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())
    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        v1_key = _original_key_for(document, version=1)

    v2_job_id = await _stage_replacement(
        sessions, storage, document_id=document_id, version=2, payload=_pdf(_V2_TEXT)
    )

    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("swap failed")

    monkeypatch.setattr(DocumentRepository, "mark_active", _boom)

    with pytest.raises(Retry):
        await ingest_document(ctx, str(document_id), str(v2_job_id))

    assert await storage.exists(v1_key) is True
    async with sessions() as session:
        chunks = DocumentChunkRepository(session)
        # v1 is still the version serving, rows and all.
        assert await chunks.list_by_version(document_id, 1) != []
        document = await session.get(Document, document_id)
        assert document is not None and document.current_version == 1


async def test_a_stale_job_cannot_roll_the_version_pointer_backwards(sessions, storage) -> None:  # noqa: ANN001
    """R-40(4). `_begin`'s short-circuit pairs `current_version >= document_version` with
    `status is ACTIVE`, so a stale job against a *non*-ACTIVE document walks straight past
    it; without the swap guard, `delete_other_versions(keep_version=1)` would then delete
    the newer version's rows and roll the pointer back to 1.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    ctx = _ctx(sessions, storage, _StubScanner())
    await ingest_document(ctx, str(document_id), str(job_id))

    v2_job_id = await _stage_replacement(
        sessions, storage, document_id=document_id, version=2, payload=_pdf(_V2_TEXT)
    )
    await ingest_document(ctx, str(document_id), str(v2_job_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        v2_key = _original_key_for(document, version=2)
        # A redelivered v1 job arriving after the v2 swap, on a document that is no longer
        # ACTIVE — so `_begin` cannot short-circuit it.
        document.status = DocumentStatus.QUEUED
        stale = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            document_version=1,
            idempotency_key=f"ingest:{document_id}:v1:stale",
        )
        session.add(stale)
        await session.commit()
        stale_id = stale.id

    await ingest_document(ctx, str(document_id), str(stale_id))

    async with sessions() as session:
        document = await session.get(Document, document_id)
        job = await session.get(KnowledgeJob, stale_id)
        assert document is not None and job is not None
        assert document.current_version == 2
        assert job.status is JobStatus.SUCCEEDED
        chunks = DocumentChunkRepository(session)
        assert await chunks.list_by_version(document_id, 2) != []
    # Nothing was purged: v2 is still the live version.
    assert await storage.exists(v2_key) is True


# --- placement (R-31: ingest in the worker, never in the API process) ---------------


def test_no_api_module_imports_the_scanner_or_the_worker() -> None:
    api_dir = Path(__file__).resolve().parents[1] / "app" / "api"
    offenders = [
        path.name
        for path in api_dir.glob("*.py")
        for needle in ("ingestion.scanner", "workers.")
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} import worker-side ingestion code into the API process — R-31 "
        "(§8.12) requires screening and parsing to happen in the worker"
    )


# --- the operator's embedding slot (T-612, R-87) --------------------------------------


async def test_the_fingerprint_names_the_model_that_produced_the_vector(sessions, storage) -> None:  # noqa: ANN001
    """T-612's central invariant, and the one mistake here that nothing downstream detects.

    The id is resolved per job from `ModelSlot.EMBEDDING` and travels on `ChunkedDocument` to
    two places: `compute_embedding_fingerprint`, which stamps it into
    `document_chunks.metadata`, and `plan_chunk_set`, which embeds with it. If those ever
    disagree, every affected chunk records a model that did not produce its vector — and the
    staleness report reads exactly that stamp, so the corpus would be mislabelled with no
    instrument able to notice.

    Both halves are asserted here rather than only the stamp, because the stamp alone passes
    while the client silently embeds with its own configured id.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    embedder = FakeEmbeddingClient()
    ctx = _ctx(sessions, storage, _StubScanner())
    ctx["embedder"] = embedder

    async with sessions() as session:
        await set_model_override(
            session, slot=ModelSlot.EMBEDDING, model_id="operators-choice", updated_by="test"
        )
        await session.commit()

    await ingest_document(ctx, str(document_id), str(job_id))

    async with sessions() as session:
        rows = await DocumentChunkRepository(session).list_by_version(document_id, 1)
    assert rows, "the ingest wrote no chunks"
    stamped = {row.meta["embedding_model"] for row in rows}

    assert stamped == {"operators-choice"}, (
        "the fingerprint names the environment default, so the per-job resolution was lost"
    )
    assert embedder.models_used == ["operators-choice"], (
        "the vectors were produced by a different model from the one stamped on them — "
        f"stamped {stamped}, embedded with {embedder.models_used}"
    )


async def test_without_an_override_the_environment_default_still_wins(sessions, storage) -> None:  # noqa: ANN001
    """The unchanged path, asserted so the resolution cannot quietly become mandatory.

    `resolve_models` fails open to `ModelSelection.from_settings`, so a deployment with no
    `model_overrides` row — which is every deployment until an operator acts — must still
    fingerprint with `OPENAI_EMBEDDING_MODEL`.
    """
    document_id, job_id, _ = await _fixtures(sessions, storage)
    embedder = FakeEmbeddingClient()
    ctx = _ctx(sessions, storage, _StubScanner())
    ctx["embedder"] = embedder

    await ingest_document(ctx, str(document_id), str(job_id))

    configured = get_settings().openai.embedding_model
    async with sessions() as session:
        rows = await DocumentChunkRepository(session).list_by_version(document_id, 1)
    assert {row.meta["embedding_model"] for row in rows} == {configured}
    assert embedder.models_used == [configured]
