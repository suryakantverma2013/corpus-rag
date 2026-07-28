"""Replace API tests (T-209, FR-KBM-07/08, R-40).

Same harness as `test_deletion.py` — `LocalFilesystemStorage` under `tmp_path`, a recording
fake queue — so neither MinIO nor Redis is needed. What the worker then does with the v(n+1)
job lives in `test_ingest_task.py`; everything here is the *synchronous* half.

The load-bearing test is `test_replace_leaves_the_old_version_serving_until_the_worker_swaps`:
a real `HybridRetriever` query before **and** after the request, with no worker in between.
Asserting `current_version == 1` and `searchable is True` would pass even against a
retrieval layer that had started filtering `Document.status` — and replace sets the status
to `QUEUED`, so that is precisely the regression R-36(3) exists to prevent.

Every assertion is scoped to the caller the test minted (T-109).
"""

from __future__ import annotations

import hashlib
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
from app.db.repositories.jobs import SUPERSEDED, KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import HybridRetriever
from app.db.repositories.users import UserRepository
from app.rag.retrieval import RetrievalFilter
from app.services.embeddings import FakeEmbeddingClient
from app.services.jobs import JobQueueError, get_job_queue
from app.services.object_storage import LocalFilesystemStorage, get_object_storage, original_key

pytestmark = pytest.mark.usefixtures("patch_jwks")

_TEXT = "The perihelion precession of Mercury is a rare lexical anchor for this fixture."


def _pdf(filler: bytes = b"") -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n" + filler + b"%%EOF\n"


def _files(payload: bytes, filename: str = "report.pdf", mime: str = "application/pdf") -> dict:
    return {"file": (filename, payload, mime)}


# ---- fixtures ----


class _RecordingQueue:
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
    payload: bytes = b"%PDF-1.7\noriginal\n%%EOF\n",
    status: DocumentStatus = DocumentStatus.ACTIVE,
    current_version: int = 1,
    with_chunk: bool = False,
    size_bytes: int | None = None,
    deleted_at=None,  # noqa: ANN001
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
    stored = await storage.put(key, payload)

    document = Document(
        id=document_id,
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        mime_type="application/pdf",
        storage_uri=stored.uri,
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=size_bytes if size_bytes is not None else stored.size,
        status=status,
        current_version=current_version,
        searchable=status is DocumentStatus.ACTIVE,
        page_count=58,
        chunk_count=1 if with_chunk else None,
        deleted_at=deleted_at,
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


def _version_key(document: Document, *, version: int, filename: str = "original.pdf") -> str:
    return (
        f"tenants/{document.tenant_id}/kb/{document.knowledge_base_id}"
        f"/documents/{document.id}/v{version}/{filename}"
    )


# ---- the R-36(3) guarantee ----


async def test_replace_leaves_the_old_version_serving_until_the_worker_swaps(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, with_chunk=True)
    assert len(await _search(session, owner)) == 1

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(_pdf(b"new bytes ")),
        headers=headers,
    )

    assert response.status_code == 202
    # No worker has run. The previous version must answer exactly as it did before.
    assert len(await _search(session, owner)) == 1
    await session.refresh(document)
    assert document.current_version == 1
    assert document.searchable is True
    assert document.chunk_count == 1
    assert document.status is DocumentStatus.QUEUED


# ---- the 202 contract ----


async def test_replace_queues_a_new_version(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    body = (
        await client.post(
            f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
        )
    ).json()

    assert body == {
        "document_id": str(document.id),
        "job_id": body["job_id"],
        "status": DocumentStatus.QUEUED,
        "version": 2,
        "duplicate": False,
    }
    jobs = await _jobs_for(session, document.id)
    assert [(j.job_type, j.document_version, j.idempotency_key) for j in jobs] == [
        (JobType.INGEST, 2, f"ingest:{document.id}:v2")
    ]
    assert [call["job_id"] for call in queue.ingests] == [jobs[0].id]


async def test_replace_repoints_storage_and_metadata_but_not_the_version_pointer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, status=DocumentStatus.FAILED)
    document.error_message = "previous attempt failed"
    await session.flush()
    payload = _pdf(b"the replacement ")

    await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(payload, filename="policy.pdf"),
        headers=headers,
    )

    await session.refresh(document)
    assert document.storage_uri.endswith("/v2/original.pdf")
    assert document.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    assert document.size_bytes == len(payload)
    assert document.filename == "policy.pdf"
    assert document.mime_type == "application/pdf"
    assert document.status is DocumentStatus.QUEUED
    assert document.error_message is None
    # Untouched — they describe the version still serving (R-36(3)).
    assert document.current_version == 1
    assert document.page_count == 58


async def test_the_new_original_is_stored_beside_the_old_one(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """Only the worker purges v1, and only after its swap commits (R-40(3))."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    assert await storage.exists(_version_key(document, version=1, filename="original.pdf"))
    assert await storage.exists(_version_key(document, version=2))


# ---- R-40(2): permitted states ----


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.UPLOADED,
        DocumentStatus.QUEUED,
        DocumentStatus.PARSING,
        DocumentStatus.CHUNKING,
        DocumentStatus.EMBEDDING,
        DocumentStatus.INDEXING,
        DocumentStatus.DELETE_PENDING,
        DocumentStatus.DELETING,
        DocumentStatus.DELETED,
    ],
)
async def test_replace_is_409_for_a_document_that_is_not_active_or_failed(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
    status: DocumentStatus,
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, status=status)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    assert response.status_code == 409
    assert queue.ingests == []
    assert await _jobs_for(session, document.id) == []
    # Rejected before anything reached storage.
    assert not await storage.exists(_version_key(document, version=2))


async def test_replace_of_a_failed_document_is_allowed(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, status=DocumentStatus.FAILED)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    assert response.status_code == 202


# ---- authorization (R-40(2), NFR-SEC-02) ----


async def test_replace_of_a_foreign_document_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    _, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=stranger)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    assert response.status_code == 404


async def test_replace_of_an_unknown_document_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
) -> None:
    _, headers = await _caller(session, make_token)
    response = await client.post(
        f"/api/v1/documents/{uuid.uuid4()}/replace", files=_files(_pdf()), headers=headers
    )
    assert response.status_code == 404


async def test_an_admin_may_replace_another_users_document(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    document = await _seed_document(session, storage, owner_id=owner)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(_pdf(b"v2 ")),
        headers=admin_headers,
    )

    assert response.status_code == 202


async def test_replace_requires_authentication(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)
    response = await client.post(f"/api/v1/documents/{document.id}/replace", files=_files(_pdf()))
    assert response.status_code == 401


# ---- R-40(1): checksum resolution ----


async def test_replacing_with_identical_bytes_is_200_and_queues_nothing(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
) -> None:
    owner, headers = await _caller(session, make_token)
    payload = _pdf(b"unchanged ")
    document = await _seed_document(session, storage, owner_id=owner, payload=payload)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(payload), headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "document_id": str(document.id),
        "job_id": None,
        "status": DocumentStatus.ACTIVE,
        "version": 1,
        "duplicate": True,
    }
    assert queue.ingests == []
    assert await _jobs_for(session, document.id) == []
    assert not await storage.exists(_version_key(document, version=2))
    await session.refresh(document)
    assert document.status is DocumentStatus.ACTIVE


async def test_replacing_with_bytes_that_belong_to_another_live_document_is_409(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """Never R-33(3)'s `200 + duplicate:true` — answering with a different document_id is a
    lie the GUI renders as success."""
    owner, headers = await _caller(session, make_token)
    target = await _seed_document(session, storage, owner_id=owner, payload=_pdf(b"target "))
    other_payload = _pdf(b"already filed ")
    await _seed_document(session, storage, owner_id=owner, payload=other_payload)
    before = target.storage_uri

    response = await client.post(
        f"/api/v1/documents/{target.id}/replace", files=_files(other_payload), headers=headers
    )

    assert response.status_code == 409
    await session.refresh(target)
    assert target.storage_uri == before
    assert target.status is DocumentStatus.ACTIVE
    assert not await storage.exists(_version_key(target, version=2))


async def test_bytes_belonging_to_a_deleted_document_may_be_used(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """The other half of R-39(4)'s partial index: a tombstone must not block a replace."""
    from datetime import UTC, datetime

    owner, headers = await _caller(session, make_token)
    payload = _pdf(b"recycled ")
    await _seed_document(
        session,
        storage,
        owner_id=owner,
        payload=payload,
        status=DocumentStatus.DELETED,
        deleted_at=datetime.now(UTC),
    )
    target = await _seed_document(session, storage, owner_id=owner, payload=_pdf(b"target "))

    response = await client.post(
        f"/api/v1/documents/{target.id}/replace", files=_files(payload), headers=headers
    )

    assert response.status_code == 202


# ---- version targeting ----


async def test_replace_targets_the_highest_ingest_version_plus_one(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """A previous replace that failed has already burned v2; reusing it would collide on
    the unique `ingest:{doc}:v2` key."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, status=DocumentStatus.FAILED)
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

    body = (
        await client.post(
            f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v3 ")), headers=headers
        )
    ).json()

    assert body["version"] == 3
    keys = {job.idempotency_key for job in await _jobs_for(session, document.id)}
    assert f"ingest:{document.id}:v3" in keys


async def test_replace_supersedes_an_open_ingest_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """ "At most one open INGEST job per document" is the invariant the version allocation
    and the swap guard both reason from."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)
    stale = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.QUEUED,
        document_version=1,
        idempotency_key=f"ingest:{document.id}:v1",
    )
    session.add(stale)
    await session.flush()

    await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    await session.refresh(stale)
    assert stale.status is JobStatus.FAILED
    assert stale.error_code == SUPERSEDED
    open_jobs = await KnowledgeJobRepository(session).list_open_for_document(
        document.id, job_type=JobType.INGEST
    )
    assert [job.document_version for job in open_jobs] == [2]


# ---- FR-ERR-02 quota ----


async def test_replace_credits_the_bytes_being_replaced(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain upload check double-counts: it adds the new original without subtracting
    the one it supersedes."""
    from app.config import get_settings

    owner, headers = await _caller(session, make_token)
    payload = _pdf(b"v2 ")
    document = await _seed_document(session, storage, owner_id=owner, size_bytes=1000)
    # used=1000, new=len(payload). A naive `used + new` exceeds the quota; the correct
    # `used - old + new` does not.
    monkeypatch.setattr(get_settings().upload, "user_quota_bytes", 1000 + len(payload) - 1)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(payload), headers=headers
    )

    assert response.status_code == 202


async def test_replace_over_the_quota_is_507(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner, size_bytes=10)
    monkeypatch.setattr(get_settings().upload, "user_quota_bytes", 11)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(_pdf(b"much larger " * 20)),
        headers=headers,
    )

    assert response.status_code == 507
    assert not await storage.exists(_version_key(document, version=2))


async def test_replace_charges_the_document_owner_not_the_admin(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin acting on someone else's document must debit the owner's allowance."""
    from app.config import get_settings

    owner, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    # A second, untouched document keeps the owner's usage above the allowance even after
    # the replaced document's own bytes are credited back.
    await _seed_document(
        session, storage, owner_id=owner, payload=_pdf(b"ballast "), size_bytes=5000
    )
    document = await _seed_document(session, storage, owner_id=owner, size_bytes=10)
    monkeypatch.setattr(get_settings().upload, "user_quota_bytes", 5001)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(_pdf(b"v2 ")),
        headers=admin_headers,
    )

    assert response.status_code == 507


# ---- rejections leave no object ----


async def test_replace_rejects_an_oversized_file(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)
    monkeypatch.setattr(get_settings().upload, "max_file_bytes", 128)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(_pdf(b"x" * 512)),
        headers=headers,
    )

    assert response.status_code == 413
    assert not await storage.exists(_version_key(document, version=2))


async def test_replace_rejects_an_unsupported_type(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace",
        files=_files(b"MZ\x90\x00executable", filename="report.pdf"),
        headers=headers,
    )

    assert response.status_code == 415
    assert not await storage.exists(_version_key(document, version=2))


async def test_replace_rejects_an_empty_file(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(b""), headers=headers
    )

    assert response.status_code == 400
    assert not await storage.exists(_version_key(document, version=2))


async def test_a_failed_replace_leaves_no_object_and_no_row_change(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compensation path: the put succeeded, the transaction did not."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)
    # Captured *before* the request: the handler's compensating `session.rollback()`
    # expires the identity map, and reading an ORM attribute afterwards from this sync
    # helper would trigger a lazy load outside the greenlet context.
    document_id = document.id
    v2_key = _version_key(document, version=2)
    before_uri, before_checksum = document.storage_uri, document.checksum_sha256

    async def _boom(self, instance):  # noqa: ANN001, ANN202
        raise RuntimeError("job insert failed")

    monkeypatch.setattr(KnowledgeJobRepository, "add", _boom)

    with pytest.raises(RuntimeError):
        await client.post(
            f"/api/v1/documents/{document_id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
        )

    assert not await storage.exists(v2_key)
    refreshed = await session.get(Document, document_id)
    assert refreshed is not None
    assert refreshed.storage_uri == before_uri
    assert refreshed.checksum_sha256 == before_checksum
    assert refreshed.status is DocumentStatus.ACTIVE


# ---- audit + enqueue ----


async def test_replace_records_a_document_replace_audit_event(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
) -> None:
    """Scoped to this test's actor — the audit table is append-only and never truncated
    between runs (T-109)."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    stmt = select(AuditLog).where(AuditLog.actor_id == owner)
    rows = list((await session.scalars(stmt)).all())
    assert [row.event_type for row in rows] == [AuditEventType.DOCUMENT_REPLACE]
    assert rows[0].target_id == str(document.id)
    # The pre-T-209 ternary filed everything that was not an upload as a deletion.
    assert AuditEventType.DOCUMENT_DELETE not in {row.event_type for row in rows}


async def test_a_failed_enqueue_still_returns_202_and_leaves_a_sweepable_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
) -> None:
    """The sweeper filters `job_type == INGEST`, so a replace stranded by a broker outage
    is rescued for free."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)
    queue.fail = True

    response = await client.post(
        f"/api/v1/documents/{document.id}/replace", files=_files(_pdf(b"v2 ")), headers=headers
    )

    assert response.status_code == 202
    jobs = await _jobs_for(session, document.id)
    assert len(jobs) == 1
    await session.refresh(jobs[0])
    assert jobs[0].error_code == "ENQUEUE_FAILED"
    assert jobs[0].job_type is JobType.INGEST
    assert jobs[0].status is JobStatus.QUEUED


async def test_replace_is_rate_limited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings().ratelimit, "upload", "2/minute")
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(session, storage, owner_id=owner)

    codes = [
        (
            await client.post(
                f"/api/v1/documents/{document.id}/replace",
                files=_files(_pdf(b"x" * n)),
                headers=headers,
            )
        ).status_code
        for n in range(3)
    ]

    assert codes[-1] == 429
