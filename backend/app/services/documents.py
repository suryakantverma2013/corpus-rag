"""Document upload orchestration (T-202, FR-ING-02).

The transaction-owning layer beneath `POST /api/v1/documents`, in the shape
`app.auth.users_service` established: repositories flush, this module commits, and typed
errors propagate to the route which maps them to status codes.

FR-ING-02 fixes the order — validate auth/permission, then type and size (FR-ERR-01/03),
then the checksum (FR-KBM-08), then store the original, then create the document and job
rows, then enqueue, then `202`. Two details are load-bearing:

**Nothing reaches storage until every rejection has fired.** R-31(3) requires the 50 MB
limit to be enforced *pre-storage*, so the size ceiling trips during the read loop, before
the first `put`. Type sniffing, quota and dedup all resolve there too, which means a
rejected upload leaves no object to clean up.

**The enqueue happens after the commit.** Enqueueing inside the transaction is the classic
dual-write bug: the worker can pick up a `document_id` that no other connection can see
yet. A failed enqueue is therefore logged and recorded on the job row, not raised — the
row is already `QUEUED` and recoverable (FR-ING-04), and the client's `202` is honest.

Malware scanning is deliberately absent: R-31 moved it to the head of the ingestion worker
(T-207) so a 50 MB scan never blocks the `202`, and R-32 selected ClamAV there.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.models.users import User
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.jobs import KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.security.content_validation import (
    UnsupportedFileTypeError,
    detect_file_type,
    sniff_family,
)
from app.services import audit
from app.services.jobs import JobQueue, JobQueueError
from app.services.object_storage import (
    ObjectStorage,
    ObjectStorageError,
    ObjectTooLargeError,
    original_key,
)

if TYPE_CHECKING:
    from fastapi import UploadFile

log = structlog.get_logger(__name__)

UploadScope = Literal["global", "chat"]

#: First version of a freshly uploaded document. T-209's replace increments it.
_INITIAL_VERSION = 1

#: Bounds uploads buffered in this process at once. `ObjectStorage.put` materialises the
#: body (a T-201 property — FR-KBM-08's checksum needs the whole thing anyway), so without
#: this a burst of concurrent 50 MB uploads is an easy memory-exhaustion vector. The
#: slowapi limit is *per user*, so it does not bound the total on its own.
_upload_slots: asyncio.Semaphore | None = None
_slots_lock = asyncio.Lock()


async def _acquire_slot(settings: Settings) -> asyncio.Semaphore:
    """Return the process-wide upload semaphore, creating it on first use."""
    global _upload_slots
    if _upload_slots is None:
        async with _slots_lock:
            if _upload_slots is None:
                _upload_slots = asyncio.Semaphore(settings.upload.max_concurrent)
    return _upload_slots


# --- errors ------------------------------------------------------------------


class UploadError(Exception):
    """Base class for upload rejections."""


class FileTooLargeError(UploadError):
    """The body exceeded FR-ERR-01's 50 MB ceiling — raised before any storage write."""


class EmptyFileError(UploadError):
    """A zero-byte upload; there is nothing to ingest."""


class QuotaExceededError(UploadError):
    """The owner's FR-ERR-02 storage allowance would be exceeded."""


class ConversationNotFoundError(UploadError):
    """No such conversation, or it belongs to another user (NFR-SEC-02)."""


class MissingConversationError(UploadError):
    """``scope="chat"`` without a ``conversation_id``."""


# --- result ------------------------------------------------------------------


class UploadOutcome:
    """What the route needs to render: the accepted `202`, or the FR-KBM-08 duplicate.

    ``duplicate`` distinguishes them. A duplicate carries the *existing* document and its
    current lifecycle status, and never a ``job_id`` — nothing was queued.
    """

    __slots__ = ("document_id", "job_id", "status", "duplicate")

    def __init__(
        self,
        *,
        document_id: uuid.UUID,
        job_id: uuid.UUID | None,
        status: DocumentStatus,
        duplicate: bool,
    ) -> None:
        self.document_id = document_id
        self.job_id = job_id
        self.status = status
        self.duplicate = duplicate


def _duplicate_of(document: Document) -> UploadOutcome:
    return UploadOutcome(
        document_id=document.id,
        job_id=None,
        status=document.status,
        duplicate=True,
    )


# --- knowledge-base resolution ------------------------------------------------


async def _resolve_knowledge_base(
    session: AsyncSession,
    *,
    user: User,
    scope: UploadScope,
    conversation_id: uuid.UUID | None,
) -> KnowledgeBase:
    """Map the R-25 two-scope model onto a concrete KB row.

    GLOBAL ↔ the owner's default KB; THIS CHAT ↔ the implicit per-conversation KB. Both
    are created on demand, so a client never has to know a KB id — which is why the route
    takes a scope rather than Source A §6's `/knowledge-bases/{id}/documents` (R-33).
    """
    repo = KnowledgeBaseRepository(session)
    if scope == "global":
        return await repo.get_or_create_default(user.id)

    if conversation_id is None:
        raise MissingConversationError("scope='chat' requires a conversation_id")

    conversation = await session.get(Conversation, conversation_id)
    # 404 rather than 403 for someone else's conversation: distinguishing the two would
    # confirm the id exists (NFR-SEC-02 per-user isolation).
    if conversation is None or conversation.owner_id != user.id:
        raise ConversationNotFoundError(str(conversation_id))

    return await repo.get_or_create_for_conversation(
        conversation_id, owner_id=user.id, tenant_id=conversation.tenant_id
    )


# --- read + validate ----------------------------------------------------------


class _ReadResult:
    __slots__ = ("payload", "size", "checksum")

    def __init__(self, payload: bytes, size: int, checksum: str) -> None:
        self.payload = payload
        self.size = size
        self.checksum = checksum


async def _read_and_hash(upload: UploadFile, settings: Settings) -> _ReadResult:
    """Single pass: measure, hash, and fail fast on size or an obvious binary.

    The size ceiling trips here — before any storage call — which is R-31(3)'s pre-storage
    enforcement requirement. Sniffing the first chunk means an executable is rejected
    without reading the remaining 50 MB.
    """
    max_bytes = settings.upload.max_file_bytes
    hasher = hashlib.sha256()
    buffer = bytearray()
    size = 0
    head_checked = False

    while chunk := await upload.read(settings.upload.read_chunk_bytes):
        size += len(chunk)
        if size > max_bytes:
            raise FileTooLargeError(f"upload exceeds {max_bytes} bytes")
        if not head_checked:
            # Raises UnsupportedFileTypeError for a binary signature; the full-payload
            # check (container members, UTF-8 validity) runs after the loop.
            sniff_family(bytes(chunk[:512]))
            head_checked = True
        hasher.update(chunk)
        buffer.extend(chunk)

    if size == 0:
        raise EmptyFileError("uploaded file is empty")
    return _ReadResult(bytes(buffer), size, hasher.hexdigest())


# --- the upload flow ----------------------------------------------------------


async def upload_document(
    *,
    upload: UploadFile,
    scope: UploadScope,
    conversation_id: uuid.UUID | None,
    user: User,
    session: AsyncSession,
    storage: ObjectStorage,
    queue: JobQueue,
    settings: Settings | None = None,
) -> UploadOutcome:
    """Validate, store, record and enqueue one upload (FR-ING-02)."""
    settings = settings or get_settings()
    filename = upload.filename or "upload"

    kb = await _resolve_knowledge_base(
        session, user=user, scope=scope, conversation_id=conversation_id
    )

    slots = await _acquire_slot(settings)
    async with slots:
        read = await _read_and_hash(upload, settings)
        detected = detect_file_type(read.payload, filename=filename)

        if settings.upload.enforce_quota:
            used = await DocumentRepository(session).total_bytes_for_owner(user.id)
            if used + read.size > settings.upload.user_quota_bytes:
                raise QuotaExceededError(
                    f"{used + read.size} bytes would exceed the "
                    f"{settings.upload.user_quota_bytes}-byte allowance"
                )

        # FR-KBM-08 fast path. The unique (knowledge_base_id, checksum_sha256) constraint
        # is what actually closes the race — see the IntegrityError branch below.
        existing = await DocumentRepository(session).find_by_checksum(
            knowledge_base_id=kb.id, checksum_sha256=read.checksum
        )
        if existing is not None:
            return _duplicate_of(existing)

        return await _store_and_record(
            read=read,
            detected_mime=detected.mime_type,
            filename=filename,
            kb=kb,
            user=user,
            session=session,
            storage=storage,
            queue=queue,
            settings=settings,
        )


async def _store_and_record(
    *,
    read: _ReadResult,
    detected_mime: str,
    filename: str,
    kb: KnowledgeBase,
    user: User,
    session: AsyncSession,
    storage: ObjectStorage,
    queue: JobQueue,
    settings: Settings,
) -> UploadOutcome:
    """Write the object, persist both rows, commit, then enqueue."""
    # Minted up front so the storage key can be built before the row exists —
    # `documents.storage_uri` is NOT NULL, so a placeholder-then-update would persist
    # a URI that points nowhere if the write failed.
    document_id = uuid.uuid4()
    key = original_key(
        tenant_id=kb.tenant_id,
        knowledge_base_id=kb.id,
        document_id=document_id,
        version=_INITIAL_VERSION,
        filename=filename,
    )

    stored = await storage.put(
        key,
        read.payload,
        content_type=detected_mime,
        max_bytes=settings.upload.max_file_bytes,
    )

    try:
        document = Document(
            id=document_id,
            tenant_id=kb.tenant_id,
            owner_id=user.id,
            knowledge_base_id=kb.id,
            filename=filename,
            mime_type=detected_mime,
            storage_uri=stored.uri,
            checksum_sha256=read.checksum,
            size_bytes=read.size,
            current_version=_INITIAL_VERSION,
            status=DocumentStatus.UPLOADED,
            searchable=False,
        )
        try:
            # A savepoint, so losing the dedup race rolls back just this insert instead
            # of poisoning the whole unit of work.
            async with session.begin_nested():
                session.add(document)
                await session.flush()
        except IntegrityError:
            return await _resolve_dedup_race(
                session=session, storage=storage, key=key, kb=kb, checksum=read.checksum
            )

        job = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            # The version the T-207 worker will build (R-38(1)). Must stay in step with
            # the `v{n}` suffix below, but this column is what the worker reads.
            document_version=_INITIAL_VERSION,
            # Deterministic and version-scoped: a T-208 retry of this version reuses the
            # key and short-circuits (FR-ING-04); T-209's replace mints a fresh one for v2.
            idempotency_key=f"ingest:{document_id}:v{_INITIAL_VERSION}",
        )
        await KnowledgeJobRepository(session).add(job)

        # Walk the FR-ING-01 transition rather than creating the row as QUEUED — the
        # document is only genuinely queued once the job row backing it exists.
        await DocumentRepository(session).set_status(document, DocumentStatus.QUEUED)

        await audit.record_document_event(
            session, actor_id=user.id, document_id=document_id, action="upload"
        )
        await session.commit()
    except Exception:
        await session.rollback()
        # The object was written before the transaction failed; leaving it would orphan
        # bytes no row references and silently consume the owner's quota.
        await _delete_quietly(storage, key)
        raise

    await _enqueue_quietly(queue, session=session, job=job, document_id=document_id)
    return UploadOutcome(
        document_id=document_id,
        job_id=job.id,
        status=DocumentStatus.QUEUED,
        duplicate=False,
    )


async def _resolve_dedup_race(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    key: str,
    kb: KnowledgeBase,
    checksum: str,
) -> UploadOutcome:
    """Turn a lost `uq_documents_knowledge_base_id_checksum_sha256` race into a duplicate.

    The `find_by_checksum` fast path can be overtaken between its read and this insert;
    the constraint is what makes FR-KBM-08 actually hold, so the winner's row is re-read
    and reported exactly as the fast path would have.
    """
    await _delete_quietly(storage, key)
    winner = await DocumentRepository(session).find_by_checksum(
        knowledge_base_id=kb.id, checksum_sha256=checksum
    )
    if winner is None:
        # The constraint fired but no row is visible — something other than the dedup
        # race failed, so do not disguise it as a duplicate.
        raise ObjectStorageError("document insert conflicted but no duplicate was found")
    log.info("upload.duplicate_race", document_id=str(winner.id), knowledge_base_id=str(kb.id))
    return _duplicate_of(winner)


async def _delete_quietly(storage: ObjectStorage, key: str) -> None:
    """Best-effort compensating delete; never masks the error being handled."""
    try:
        await storage.delete(key)
    except Exception:  # noqa: BLE001 — compensation must not replace the original failure
        log.error("upload.orphaned_object", key=key, exc_info=True)


async def _enqueue_quietly(
    queue: JobQueue, *, session: AsyncSession, job: KnowledgeJob, document_id: uuid.UUID
) -> None:
    """Dispatch after commit; record a failure on the job rather than failing the upload.

    The row is committed and `QUEUED`, so the honest answer to the client is still `202`.
    Marking `error_code` gives `GET /jobs/{id}` (FR-ING-06) something to show and gives
    T-207's sweeper a way to find jobs that were never dispatched.
    """
    try:
        await queue.enqueue_ingest(
            job_id=job.id, document_id=document_id, idempotency_key=job.idempotency_key
        )
    except JobQueueError as exc:
        log.error(
            "upload.enqueue_failed",
            job_id=str(job.id),
            document_id=str(document_id),
            error=str(exc),
        )
        try:
            job.error_code = "ENQUEUE_FAILED"
            job.error_message = str(exc)
            await session.commit()
        except Exception:  # noqa: BLE001 — diagnostics only; the upload already succeeded
            await session.rollback()
            log.error("upload.enqueue_failure_not_recorded", job_id=str(job.id), exc_info=True)


__all__ = [
    "ConversationNotFoundError",
    "EmptyFileError",
    "FileTooLargeError",
    "MissingConversationError",
    "ObjectTooLargeError",
    "QuotaExceededError",
    "UnsupportedFileTypeError",
    "UploadError",
    "UploadOutcome",
    "UploadScope",
    "upload_document",
]
