"""Deletion and retry API tests (T-208, FR-ING-05/06, R-39).

Same harness as `test_upload.py` — object storage on `LocalFilesystemStorage` under
`tmp_path`, the job queue on a recording fake — so neither MinIO nor Redis is needed. What
the worker then does with the queued job is `test_delete_task.py`; everything here is the
*synchronous* half, which is the half FR-ING-05 makes promises about.

The load-bearing test is `test_delete_removes_the_document_from_retrieval_immediately`: it
runs a real `HybridRetriever` query before and after the `DELETE`, with no worker in
between. Asserting `searchable is False` would pass even if `_access_predicates` ignored the
column — and it did ignore it until T-206 (see R-37(8)), which is exactly the failure this
requirement exists to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import AuditEventType, DocumentStatus, JobStatus, JobType
from app.db.models.audit_log import AuditLog
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.jobs import DOCUMENT_DELETED
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import HybridRetriever
from app.db.repositories.users import UserRepository
from app.rag.retrieval import RetrievalFilter
from app.services.embeddings import FakeEmbeddingClient
from app.services.jobs import JobQueueError, get_job_queue
from app.services.object_storage import LocalFilesystemStorage, get_object_storage, original_key

pytestmark = pytest.mark.usefixtures("patch_jwks")

_TEXT = "The perihelion precession of Mercury is a rare lexical anchor for this fixture."


# ---- fixtures ----


class _RecordingQueue:
    """Fake JobQueue recording both dispatch kinds, or raising when `fail` is set."""

    def __init__(self) -> None:
        self.ingests: list[dict[str, object]] = []
        self.deletes: list[dict[str, object]] = []
        self.fail = False

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self._record(self.ingests, job_id, document_id, idempotency_key)

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self._record(self.deletes, job_id, document_id, idempotency_key)

    def _record(
        self,
        sink: list[dict[str, object]],
        job_id: uuid.UUID,
        document_id: uuid.UUID,
        idempotency_key: str,
    ) -> None:
        if self.fail:
            raise JobQueueError("broker down")
        sink.append(
            {"job_id": job_id, "document_id": document_id, "idempotency_key": idempotency_key}
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def storage(tmp_path) -> LocalFilesystemStorage:  # noqa: ANN001
    return LocalFilesystemStorage(tmp_path / "objects")


@pytest.fixture
def queue() -> _RecordingQueue:
    return _RecordingQueue()


@pytest.fixture
def app(app, storage: LocalFilesystemStorage, queue: _RecordingQueue):  # noqa: ANN001, ANN201
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    """Seed a caller's local row and return (user id, auth headers)."""
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _seed_document(
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    *,
    owner_id: uuid.UUID,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    current_version: int = 1,
    with_chunk: bool = False,
) -> Document:
    """A document row plus its stored original, and optionally one embedded chunk."""
    document_id = uuid.uuid4()
    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    key = original_key(
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=kb.id,
        document_id=document_id,
        version=current_version,
        filename="handbook.pdf",
    )
    stored = await storage.put(key, b"%PDF-1.7\n%%EOF\n")

    document = Document(
        id=document_id,
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        storage_uri=stored.uri,
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=stored.size,
        status=status,
        current_version=current_version,
        searchable=status is DocumentStatus.ACTIVE,
        chunk_count=1 if with_chunk else None,
    )
    session.add(document)
    await session.flush()

    if with_chunk:
        session.add(
            DocumentChunk(
                document_id=document_id,
                document_version=current_version,
                chunk_index=0,
                chunk_hash=uuid.uuid4().hex * 2,
                embedding_fingerprint=uuid.uuid4().hex * 2,
                token_count=20,
                tenant_id=DEFAULT_TENANT_ID,
                knowledge_base_id=kb.id,
                chunk_text=_TEXT,
                embedding=await FakeEmbeddingClient().embed_query(_TEXT),
                meta={"block_order": 0, "block_chunk_index": 0},
            )
        )
        await session.flush()
    return document


async def _search(session: AsyncSession, owner_id: uuid.UUID) -> list:
    embedder = FakeEmbeddingClient()
    return await HybridRetriever(session).search(
        "perihelion precession of Mercury",
        await embedder.embed_query("perihelion precession of Mercury"),
        filters=RetrievalFilter(owner_id=owner_id),
    )


async def _jobs_for(session: AsyncSession, document_id: uuid.UUID) -> list[KnowledgeJob]:
    stmt = select(KnowledgeJob).where(KnowledgeJob.document_id == document_id)
    return list((await session.scalars(stmt)).all())


# ---- FR-ING-05: the synchronous half ----


async def test_delete_removes_the_document_from_retrieval_immediately(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    """FR-ING-05's actual promise, checked with a query rather than with a flag."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id, with_chunk=True)

    assert await _search(session, owner_id), "fixture is not retrievable to begin with"

    response = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    assert response.status_code == 202
    # No worker has run — this is purely what the request itself committed.
    assert await _search(session, owner_id) == []


async def test_delete_returns_the_pending_state_and_queues_one_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)

    body = (await client.delete(f"/api/v1/documents/{document.id}", headers=headers)).json()

    assert body["status"] == DocumentStatus.DELETE_PENDING
    assert body["already_deleted"] is False
    await session.refresh(document)
    assert document.status is DocumentStatus.DELETE_PENDING
    assert document.searchable is False
    assert document.deleted_at is None, "deleted_at belongs to the worker's terminal commit"

    jobs = await _jobs_for(session, document.id)
    assert [job.job_type for job in jobs] == [JobType.DELETE]
    assert jobs[0].status is JobStatus.QUEUED
    assert jobs[0].idempotency_key == f"delete:{document.id}"
    assert [call["job_id"] for call in queue.deletes] == [uuid.UUID(body["job_id"])]
    assert queue.ingests == []


async def test_deleting_twice_re_drives_the_same_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    """R-39(2): the re-drive for a dead-lettered purge, not a second job row."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)

    first = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)
    # Simulate the purge having exhausted its retries.
    jobs = await _jobs_for(session, document.id)
    jobs[0].status = JobStatus.DEAD_LETTER
    jobs[0].error_code = "OBJECT_STORAGE_UNAVAILABLE"
    await session.flush()

    second = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(await _jobs_for(session, document.id)) == 1
    await session.refresh(jobs[0])
    assert jobs[0].status is JobStatus.QUEUED
    assert jobs[0].error_code is None
    assert len(queue.deletes) == 2


async def test_deleting_an_already_deleted_document_is_200(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.DELETED
    )

    response = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["already_deleted"] is True
    assert response.json()["job_id"] is None
    assert queue.deletes == []


async def test_delete_supersedes_an_open_ingestion_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    """§11 'delete while ingestion is running', first of R-39(8)'s three points."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.PARSING
    )
    ingest = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.RUNNING,
        document_version=1,
        idempotency_key=f"ingest:{document.id}:v1",
    )
    session.add(ingest)
    await session.flush()

    await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    await session.refresh(ingest)
    assert ingest.status is JobStatus.FAILED
    assert ingest.error_code == DOCUMENT_DELETED


async def test_delete_records_an_audit_event(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)

    await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    # Scoped to this test's actor, per the convention `test_audit.py` documents.
    stmt = select(AuditLog).where(
        AuditLog.actor_id == owner_id, AuditLog.event_type == AuditEventType.DOCUMENT_DELETE
    )
    events = list((await session.scalars(stmt)).all())
    assert [event.target_id for event in events] == [str(document.id)]


async def test_a_failed_enqueue_still_returns_202(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    """The row is committed and the document has already left retrieval — `202` is honest."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)
    queue.fail = True

    response = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)

    assert response.status_code == 202
    await session.refresh(document)
    assert document.searchable is False
    jobs = await _jobs_for(session, document.id)
    assert jobs[0].error_code == "ENQUEUE_FAILED"


# ---- R-39(1): authorization ----


async def test_a_foreign_document_is_404_not_403(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    """NFR-SEC-02: a distinguishable 403 would confirm the id exists."""
    owner_id, _ = await _caller(session, make_token)
    _, intruder_headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)

    response = await client.delete(f"/api/v1/documents/{document.id}", headers=intruder_headers)

    assert response.status_code == 404
    await session.refresh(document)
    assert document.status is DocumentStatus.ACTIVE
    assert queue.deletes == []


async def test_an_owner_may_delete_their_own_document(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    """R-39(1) amending FR-USR-06 — the point of resolving OI-20."""
    owner_id, headers = await _caller(session, make_token, admin=False)
    document = await _seed_document(session, storage, owner_id=owner_id)

    assert (
        await client.delete(f"/api/v1/documents/{document.id}", headers=headers)
    ).status_code == 202


async def test_an_admin_may_delete_another_users_document(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    """FR-USR-04 — unrestricted access survives the amendment."""
    owner_id, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    document = await _seed_document(session, storage, owner_id=owner_id)

    response = await client.delete(f"/api/v1/documents/{document.id}", headers=admin_headers)

    assert response.status_code == 202
    await session.refresh(document)
    assert document.status is DocumentStatus.DELETE_PENDING


async def test_delete_requires_authentication(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, _ = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id)

    assert (await client.delete(f"/api/v1/documents/{document.id}")).status_code == 401


# ---- FR-ING-06: retry ----


async def test_retry_of_a_failed_document_queues_a_fresh_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    queue,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.FAILED
    )
    document.error_message = "CORRUPT_DOCUMENT: could not parse"
    failed = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.FAILED,
        document_version=1,
        error_code="CORRUPT_DOCUMENT",
        idempotency_key=f"ingest:{document.id}:v1",
    )
    session.add(failed)
    await session.flush()

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] == DocumentStatus.QUEUED
    await session.refresh(document)
    assert document.status is DocumentStatus.QUEUED
    assert document.error_message is None, "a re-queued document must stop showing the old reason"

    jobs = sorted(await _jobs_for(session, document.id), key=lambda job: job.idempotency_key)
    # The failed row survives: it is the record `GET /jobs/{id}` renders (R-39(5)).
    assert [job.idempotency_key for job in jobs] == [
        f"ingest:{document.id}:v1",
        f"ingest:{document.id}:v1:r1",
    ]
    assert jobs[0].status is JobStatus.FAILED
    assert jobs[1].status is JobStatus.QUEUED
    assert jobs[1].document_version == 1
    assert [call["job_id"] for call in queue.ingests] == [uuid.UUID(response.json()["job_id"])]


async def test_retry_targets_the_version_the_failed_job_was_building(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    """R-39(5): not `current_version`, which R-36(3) leaves on the version still serving."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.FAILED, current_version=1
    )
    session.add(
        KnowledgeJob(
            document_id=document.id,
            job_type=JobType.INGEST,
            status=JobStatus.FAILED,
            document_version=2,
            idempotency_key=f"ingest:{document.id}:v2",
        )
    )
    await session.flush()

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    new_job = await session.get(KnowledgeJob, uuid.UUID(response.json()["job_id"]))
    assert new_job is not None
    assert new_job.document_version == 2
    assert new_job.idempotency_key == f"ingest:{document.id}:v2:r1"


async def test_repeated_retries_never_collide_on_the_idempotency_key(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.FAILED
    )
    session.add(
        KnowledgeJob(
            document_id=document.id,
            job_type=JobType.INGEST,
            status=JobStatus.FAILED,
            document_version=1,
            idempotency_key=f"ingest:{document.id}:v1",
        )
    )
    await session.flush()

    keys = []
    for _ in range(3):
        response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)
        assert response.status_code == 202
        keys.append(response.json()["job_id"])
        # Each attempt fails again, returning the document to the only retryable state.
        await session.refresh(document)
        document.status = DocumentStatus.FAILED
        await session.flush()

    assert len(set(keys)) == 3
    all_keys = {job.idempotency_key for job in await _jobs_for(session, document.id)}
    assert all_keys == {
        f"ingest:{document.id}:v1",
        f"ingest:{document.id}:v1:r1",
        f"ingest:{document.id}:v1:r2",
        f"ingest:{document.id}:v1:r3",
    }


@pytest.mark.parametrize(
    "status",
    [DocumentStatus.ACTIVE, DocumentStatus.PARSING, DocumentStatus.DELETE_PENDING],
)
async def test_retry_is_409_for_anything_but_failed(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,  # noqa: ANN001
    queue,  # noqa: ANN001
    make_token,  # noqa: ANN001
    status: DocumentStatus,
) -> None:
    """R-38(5) keeps a *retryable* failure in its in-flight state, so those are still live."""
    owner_id, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner_id, status=status)

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    assert response.status_code == 409
    assert queue.ingests == []


async def test_retry_of_a_foreign_document_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage,
    make_token,  # noqa: ANN001
) -> None:
    owner_id, _ = await _caller(session, make_token)
    _, intruder_headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner_id, status=DocumentStatus.FAILED
    )

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=intruder_headers)

    assert response.status_code == 404


async def test_retry_of_an_unknown_document_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)

    response = await client.post(f"/api/v1/documents/{uuid.uuid4()}/retry", headers=headers)

    assert response.status_code == 404
