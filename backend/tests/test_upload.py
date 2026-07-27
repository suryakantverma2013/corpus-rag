"""Upload API tests (T-202, FR-ING-02).

Object storage is pointed at a `LocalFilesystemStorage` under `tmp_path` and the job
queue at a recording fake, both via `dependency_overrides` — so the suite needs neither
MinIO nor Redis. Uploads are ordinary non-admin operations (FR-USR-06 restricts *deletion*
to admins), so callers here carry only the `user` role.

The R-31 regression tests are the point of this file: an oversize body must leave storage
untouched, and a spoofed extension must be rejected on content rather than on its name.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AuditEventType, DocumentStatus, JobStatus, JobType, KBVisibility
from app.db.models.audit_log import AuditLog
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.users import UserRepository
from app.services.jobs import JobQueueError, get_job_queue
from app.services.object_storage import LocalFilesystemStorage, get_object_storage

pytestmark = pytest.mark.usefixtures("patch_jwks")


# ---- payload builders ----


def _pdf(filler: bytes = b"") -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n" + filler + b"%%EOF\n"


def _docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<?xml version='1.0'?><Types/>")
        archive.writestr("word/document.xml", b"<?xml version='1.0'?><document><body/></document>")
    return buffer.getvalue()


def _files(payload: bytes, filename: str = "report.pdf", mime: str = "application/pdf") -> dict:
    return {"file": (filename, payload, mime)}


# ---- fixtures ----


class _RecordingQueue:
    """Fake JobQueue: records dispatches, or raises when `fail` is set."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = False

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        if self.fail:
            raise JobQueueError("broker down")
        self.calls.append(
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
    """Extend the shared app fixture with storage + queue overrides."""
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


async def _owner(
    session: AsyncSession, make_token: Callable[..., str], *, email: str = "user@corpus.local"
) -> tuple[uuid.UUID, dict[str, str]]:
    """Seed a non-admin caller's local row and return (sub, auth headers)."""
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    token = make_token(sub=sub, email=email, roles=("user",))
    return sub, {"Authorization": f"Bearer {token}"}


async def _document(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await session.get(Document, document_id)


async def _documents_of(session: AsyncSession, owner_id: uuid.UUID) -> list[Document]:
    """Scope assertions to the caller's own rows.

    The suite runs against the shared local `corpus` database, so asserting over the whole
    table couples every test to whatever else happens to be in it.
    """
    return list(
        (await session.scalars(select(Document).where(Document.owner_id == owner_id))).all()
    )


async def _jobs_of(session: AsyncSession, owner_id: uuid.UUID) -> list[KnowledgeJob]:
    stmt = (
        select(KnowledgeJob)
        .join(Document, Document.id == KnowledgeJob.document_id)
        .where(Document.owner_id == owner_id)
    )
    return list((await session.scalars(stmt)).all())


def _stored_files(storage: LocalFilesystemStorage) -> list[str]:
    import os

    if not os.path.isdir(storage.root):
        return []
    return [os.path.join(root, name) for root, _, files in os.walk(storage.root) for name in files]


# ---- Authz ----


async def test_upload_requires_token(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/documents", files=_files(_pdf()))
    assert resp.status_code == 401


async def test_upload_rejected_for_inactive_user(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    sub, headers = await _owner(session, make_token)
    user = await UserRepository(session).get(sub)
    assert user is not None
    user.is_active = False
    await session.flush()

    resp = await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)
    assert resp.status_code == 403


# ---- Happy path, GLOBAL scope ----


async def test_upload_pdf_persists_rows_and_object(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    queue: _RecordingQueue,
) -> None:
    sub, headers = await _owner(session, make_token)
    payload = _pdf()

    resp = await client.post("/api/v1/documents", files=_files(payload), headers=headers)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["duplicate"] is False
    assert body["job_id"] is not None

    document = await _document(session, uuid.UUID(body["document_id"]))
    assert document is not None
    assert document.status is DocumentStatus.QUEUED
    assert document.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    assert document.size_bytes == len(payload)
    assert document.searchable is False
    assert document.current_version == 1
    assert document.mime_type == "application/pdf"
    assert document.owner_id == sub

    # The object is where the T-201 key layout says, and byte-identical.
    key = storage.key_for_uri(document.storage_uri)
    assert key.endswith(f"/documents/{document.id}/v1/original.pdf")
    assert await storage.get(key) == payload

    job = (
        await session.scalars(select(KnowledgeJob).where(KnowledgeJob.document_id == document.id))
    ).one()
    assert (job.job_type, job.status) == (JobType.INGEST, JobStatus.QUEUED)
    assert job.idempotency_key == f"ingest:{document.id}:v1"

    # Enqueued once, after the commit, with the row's own idempotency key (FR-ING-04).
    assert len(queue.calls) == 1
    assert queue.calls[0]["document_id"] == document.id
    assert queue.calls[0]["idempotency_key"] == job.idempotency_key


@pytest.mark.parametrize(
    ("payload", "filename", "mime"),
    [
        (_pdf(), "report.pdf", "application/pdf"),
        (_docx(), "report.docx", "application/octet-stream"),
        (b"a,b,c\n1,2,3\n", "table.csv", "text/csv"),
        (b"# Notes\n\nbody\n", "notes.md", "text/markdown"),
    ],
)
async def test_all_four_formats_accepted(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    payload: bytes,
    filename: str,
    mime: str,
) -> None:
    _, headers = await _owner(session, make_token)
    resp = await client.post(
        "/api/v1/documents", files=_files(payload, filename, mime), headers=headers
    )
    assert resp.status_code == 202, resp.text


async def test_upload_creates_default_kb_and_audit_row(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    sub, headers = await _owner(session, make_token)
    resp = await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)
    assert resp.status_code == 202
    document_id = uuid.UUID(resp.json()["document_id"])

    kb = (await session.scalars(select(KnowledgeBase).where(KnowledgeBase.owner_id == sub))).one()
    assert kb.visibility is KBVisibility.GLOBAL
    assert kb.conversation_id is None

    entry = (
        await session.scalars(select(AuditLog).where(AuditLog.target_id == str(document_id)))
    ).one()
    assert entry.event_type is AuditEventType.DOCUMENT_UPLOAD
    assert entry.actor_id == sub


# ---- Chat scope (R-25 implicit per-conversation KB) ----


async def _conversation(session: AsyncSession, owner_id: uuid.UUID) -> Conversation:
    conversation = Conversation(owner_id=owner_id, title="Chat")
    session.add(conversation)
    await session.flush()
    return conversation


async def test_chat_scope_creates_and_reuses_conversation_kb(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    sub, headers = await _owner(session, make_token)
    conversation = await _conversation(session, sub)
    data = {"scope": "chat", "conversation_id": str(conversation.id)}

    first = await client.post(
        "/api/v1/documents", files=_files(_pdf(b"one")), data=data, headers=headers
    )
    second = await client.post(
        "/api/v1/documents", files=_files(_pdf(b"two")), data=data, headers=headers
    )
    assert (first.status_code, second.status_code) == (202, 202)

    kbs = list(
        (
            await session.scalars(
                select(KnowledgeBase).where(KnowledgeBase.conversation_id == conversation.id)
            )
        ).all()
    )
    assert len(kbs) == 1
    assert kbs[0].visibility is KBVisibility.CONVERSATION

    documents = list(
        (
            await session.scalars(select(Document).where(Document.knowledge_base_id == kbs[0].id))
        ).all()
    )
    assert len(documents) == 2


async def test_chat_scope_requires_conversation_id(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _owner(session, make_token)
    resp = await client.post(
        "/api/v1/documents", files=_files(_pdf()), data={"scope": "chat"}, headers=headers
    )
    assert resp.status_code == 400
    assert "conversation_id" in resp.json()["detail"]


async def test_chat_scope_foreign_conversation_is_404(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Another user's conversation must not be distinguishable from a missing one."""
    other_sub, _ = await _owner(session, make_token, email="other@corpus.local")
    conversation = await _conversation(session, other_sub)
    _, headers = await _owner(session, make_token)

    resp = await client.post(
        "/api/v1/documents",
        files=_files(_pdf()),
        data={"scope": "chat", "conversation_id": str(conversation.id)},
        headers=headers,
    )
    assert resp.status_code == 404

    missing = await client.post(
        "/api/v1/documents",
        files=_files(_pdf()),
        data={"scope": "chat", "conversation_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == resp.json()["detail"]


# ---- R-31 controls ----


async def test_oversize_rejected_before_any_storage_write(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-31(3): the 50 MB ceiling is enforced *pre-storage*, so nothing is written."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.upload, "max_file_bytes", 1024)
    sub, headers = await _owner(session, make_token)

    resp = await client.post("/api/v1/documents", files=_files(_pdf(b"x" * 4096)), headers=headers)

    assert resp.status_code == 413
    assert "50 MB" in resp.json()["detail"]
    assert _stored_files(storage) == []
    assert await _documents_of(session, sub) == []


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (b"MZ\x90\x00\x03" + b"\x00" * 512, "report.pdf"),  # PE executable named .pdf
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 512, "notes.md"),  # ELF named .md
        (_pdf(), "notes.md"),  # real PDF named .md
        (_pdf(), "table.csv"),
        (b"not a pdf at all\n", "report.pdf"),  # text named .pdf
        (b"PK\x03\x04nonsense", "report.docx"),  # broken ZIP named .docx
        (b"col\n\x00\x00binary\n", "table.csv"),  # NUL bytes named .csv
    ],
)
async def test_extension_spoofing_rejected(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    payload: bytes,
    filename: str,
) -> None:
    """R-31(3): type is decided by content, never by the attacker-supplied extension."""
    _, headers = await _owner(session, make_token)
    resp = await client.post("/api/v1/documents", files=_files(payload, filename), headers=headers)
    assert resp.status_code == 415, resp.text
    assert _stored_files(storage) == []


async def test_plain_zip_named_docx_rejected(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", b"hello")
    _, headers = await _owner(session, make_token)

    resp = await client.post(
        "/api/v1/documents", files=_files(buffer.getvalue(), "report.docx"), headers=headers
    )
    assert resp.status_code == 415


async def test_unaccepted_extension_rejected(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _owner(session, make_token)
    resp = await client.post(
        "/api/v1/documents", files=_files(b"plain text\n", "notes.txt"), headers=headers
    )
    assert resp.status_code == 415


async def test_empty_file_rejected(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _owner(session, make_token)
    resp = await client.post("/api/v1/documents", files=_files(b"", "table.csv"), headers=headers)
    assert resp.status_code == 400


# ---- Duplicates (FR-KBM-08) ----


async def test_duplicate_returns_200_and_does_not_reingest(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    queue: _RecordingQueue,
) -> None:
    sub, headers = await _owner(session, make_token)
    payload = _pdf()

    first = await client.post("/api/v1/documents", files=_files(payload), headers=headers)
    stored_after_first = _stored_files(storage)
    second = await client.post("/api/v1/documents", files=_files(payload), headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["job_id"] is None
    assert body["document_id"] == first.json()["document_id"]
    assert body["status"] == "QUEUED"

    # Nothing was re-ingested: one row, one job, one object, one dispatch.
    assert len(await _documents_of(session, sub)) == 1
    assert len(await _jobs_of(session, sub)) == 1
    assert _stored_files(storage) == stored_after_first
    assert len(queue.calls) == 1


async def test_same_bytes_in_different_kb_are_not_duplicates(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-KBM-08 dedup is scoped per knowledge base, not per user."""
    sub, headers = await _owner(session, make_token)
    conversation = await _conversation(session, sub)
    payload = _pdf()

    globally = await client.post("/api/v1/documents", files=_files(payload), headers=headers)
    in_chat = await client.post(
        "/api/v1/documents",
        files=_files(payload),
        data={"scope": "chat", "conversation_id": str(conversation.id)},
        headers=headers,
    )
    assert (globally.status_code, in_chat.status_code) == (202, 202)
    assert globally.json()["document_id"] != in_chat.json()["document_id"]


# ---- Quota (FR-ERR-02) ----


async def test_quota_exceeded_rejected(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    _, headers = await _owner(session, make_token)
    # First upload succeeds, then the allowance drops below what a second would need.
    first = await client.post("/api/v1/documents", files=_files(_pdf(b"a")), headers=headers)
    assert first.status_code == 202

    monkeypatch.setattr(get_settings().upload, "user_quota_bytes", 1)
    stored_before = _stored_files(storage)

    resp = await client.post("/api/v1/documents", files=_files(_pdf(b"b")), headers=headers)
    assert resp.status_code == 507
    assert "10 GB" in resp.json()["detail"]
    assert _stored_files(storage) == stored_before


async def test_quota_can_be_disabled(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings().upload, "user_quota_bytes", 1)
    monkeypatch.setattr(get_settings().upload, "enforce_quota", False)
    _, headers = await _owner(session, make_token)

    resp = await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)
    assert resp.status_code == 202


# ---- Rate limiting (T-105, NFR-SEC-07) ----


async def test_upload_rate_limited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings().ratelimit, "upload", "2/minute")
    _, headers = await _owner(session, make_token)

    codes = [
        (
            await client.post(
                "/api/v1/documents", files=_files(_pdf(str(n).encode())), headers=headers
            )
        ).status_code
        for n in range(3)
    ]
    assert codes[:2] == [202, 202]
    assert codes[2] == 429


# ---- Failure handling ----


async def test_enqueue_failure_still_returns_202(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
) -> None:
    """The row is committed and QUEUED, so a broker outage must not fail the upload."""
    queue.fail = True
    sub, headers = await _owner(session, make_token)

    resp = await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)

    assert resp.status_code == 202
    (job,) = await _jobs_of(session, sub)
    assert job.status is JobStatus.QUEUED
    assert job.error_code == "ENQUEUE_FAILED"


async def test_storage_failure_leaves_no_document_row(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.object_storage import ObjectStorageError

    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ObjectStorageError("bucket unreachable")

    monkeypatch.setattr(storage, "put", _boom)
    sub, headers = await _owner(session, make_token)

    resp = await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)

    assert resp.status_code == 503
    assert await _documents_of(session, sub) == []


async def test_commit_failure_removes_the_stored_object(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    storage: LocalFilesystemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed transaction must not orphan bytes that still count against the quota."""
    original_commit = AsyncSession.commit
    calls = {"n": 0}

    async def _failing_commit(self: AsyncSession) -> None:
        calls["n"] += 1
        raise RuntimeError("commit failed")

    monkeypatch.setattr(AsyncSession, "commit", _failing_commit)
    _, headers = await _owner(session, make_token)

    with pytest.raises(RuntimeError):
        await client.post("/api/v1/documents", files=_files(_pdf()), headers=headers)

    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    assert calls["n"] >= 1
    assert _stored_files(storage) == []
