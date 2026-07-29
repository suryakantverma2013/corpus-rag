"""Document lifecycle orchestration — upload (T-202, FR-ING-02), deletion and retry
(T-208, FR-ING-05/06, R-39), and replace (T-209, FR-KBM-07/08, R-40).

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

The deletion, retry and replace flows at the foot of this module follow the same two rules
— nothing irreversible before the request can still fail, and the enqueue after the commit.
Deletion adds a third: it takes the document row `FOR UPDATE`, because the thing it races is
not another request but the ingestion worker's swap (R-39(8)). Replace takes the same lock
for the same reason, and adds a fourth rule of its own: it writes only the fields that
describe the *new* bytes, never the ones that describe the version still serving retrieval.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

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
from app.db.repositories.jobs import DOCUMENT_DELETED, SUPERSEDED, KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.processing_lock import ProcessingLockRepository
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


class _Enqueue(Protocol):
    """One bound `JobQueue.enqueue_*` method — what `_enqueue_quietly` dispatches."""

    async def __call__(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None: ...


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


class DocumentNotFoundError(Exception):
    """No such document, or it belongs to another user (R-39(1), NFR-SEC-02 → 404)."""


class NotRetryableError(Exception):
    """Retry requested for a document that is not `FAILED` (R-39(5) → 409)."""


class NotReplaceableError(Exception):
    """Replace requested for a document that is not `ACTIVE` or `FAILED` (R-40(2) → 409)."""


class DuplicateChecksumError(Exception):
    """The new bytes already belong to a *different* live document in this KB (R-40(1) → 409).

    Deliberately not R-33(3)'s `200 + duplicate:true`: the caller asked to change *this*
    document, and answering with a different `document_id` is a lie the GUI renders as
    success.
    """


class ProcessingLockedError(Exception):
    """The caller has a chat turn generating; FR-STA-02 pauses this action (R-43 → 409).

    Carries the conversation that holds the gate so the route can say *which* chat is busy —
    the caller may have several open, and "you can't upload right now" without saying why is
    the version of this message users file bugs about.
    """

    def __init__(self, conversation_id: uuid.UUID | None) -> None:
        super().__init__("a response is being generated for this user")
        self.conversation_id = conversation_id


# --- the R-24 action gate ------------------------------------------------------


async def _reject_if_processing(
    session: AsyncSession, *, owner_id: uuid.UUID, settings: Settings | None = None
) -> None:
    """FR-STA-02 / FR-ORC-04 — refuse a mutating file operation mid-turn (R-24, R-43).

    Called first in each of R-24's four verbs (upload, delete, retry, replace), **before**
    any side effect: `_resolve_knowledge_base` creates a KB on demand and `replace_document`
    buffers 50 MB, neither of which should happen for a request that is about to be refused.

    Read-only, and keyed on the **caller**. There is no admin exemption because none is
    needed: the gate an admin trips is their own in-flight turn, never the document owner's.
    Read routes are never gated — FR-STA-02 names only these four verbs, and a knowledge-base
    modal that froze while the user chatted would be a spectacular misreading of it.

    The lock is **advisory** (R-43): an expired or unpublished gate simply lets the action
    through, which R-24 sanctions by placing consistency on FR-ING-04/05 + FR-RET-04 and
    citation validity on serve-time FR-CIT-06.
    """
    settings = settings or get_settings()
    if not settings.graph.lock_enforced:
        return
    held = await ProcessingLockRepository(session).active_for(owner_id)
    if held is not None:
        raise ProcessingLockedError(held.conversation_id)


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


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    """What `DELETE /documents/{id}` renders (R-39(2)).

    ``already_deleted`` selects `200` over `202`; it carries no ``job_id`` because nothing
    was queued — the same distinction `UploadOutcome.duplicate` makes.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: DocumentStatus
    already_deleted: bool


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """What `POST /documents/{id}/retry` renders. Always a fresh job (R-39(5))."""

    document_id: uuid.UUID
    job_id: uuid.UUID
    status: DocumentStatus


@dataclass(frozen=True, slots=True)
class ReplaceOutcome:
    """What `POST /documents/{id}/replace` renders (R-40(1)).

    ``version`` is the version the worker will **build**, never the one currently serving —
    `current_version` stays on the old one until the swap commits (R-36(3)). The two are
    named differently on purpose: one number for both would be the likeliest source of a
    GUI bug on this whole surface.

    ``duplicate`` selects `200` over `202` for identical bytes, mirroring
    `UploadOutcome.duplicate`; nothing was queued, so ``job_id`` is ``None`` and ``version``
    is simply the version already serving.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: DocumentStatus
    version: int
    duplicate: bool


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


async def resolve_scope_kb_id(
    session: AsyncSession,
    *,
    user: User,
    scope: UploadScope,
    conversation_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Map an FR-KBM-03 scope onto a KB id **without creating one** (T-209).

    Deliberately not `_resolve_knowledge_base`, which calls `get_or_create_*`: a `GET` that
    INSERTs is wrong twice over. It takes write locks to serve a read, and under
    `get_session` — which never commits — the row is emitted and then discarded, so the
    knowledge base appears to exist for the rest of the unit of work and then does not.

    ``None`` means "the scope is legitimate but nothing has been created in it yet", which
    the route renders as an empty list rather than a `404`.

    The two rejections are the same ones the upload route already maps, with the same
    codes: no `conversation_id` under ``scope="chat"`` → `400`, and a conversation that is
    missing *or someone else's* → `404`, never `403` (NFR-SEC-02).
    """
    repo = KnowledgeBaseRepository(session)
    if scope == "global":
        kb = await repo.get_default(user.id)
        return kb.id if kb is not None else None

    if conversation_id is None:
        raise MissingConversationError("scope='chat' requires a conversation_id")

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != user.id:
        raise ConversationNotFoundError(str(conversation_id))

    kb = await repo.get_for_conversation(conversation_id)
    return kb.id if kb is not None else None


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

    await _reject_if_processing(session, owner_id=user.id, settings=settings)

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

    await _enqueue_quietly(queue.enqueue_ingest, session=session, job=job, document_id=document_id)
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
    enqueue: _Enqueue,
    *,
    session: AsyncSession,
    job: KnowledgeJob,
    document_id: uuid.UUID,
) -> None:
    """Dispatch after commit; record a failure on the job rather than failing the request.

    The row is committed and `QUEUED`, so the honest answer to the client is still `202`.
    Marking `error_code` gives `GET /jobs/{id}` (FR-ING-06) something to show and gives
    T-207's sweeper a way to find jobs that were never dispatched.

    `enqueue` is the bound queue method rather than the queue, so deletion reuses this
    without the function having to know which kind of job it is holding. Note the sweeper
    filters `job_type == INGEST`: an undispatched **deletion** is deliberately not swept,
    because the document is already non-searchable and repeating the `DELETE` re-dispatches
    it (R-39(2)) — a second sweeper would only race that.
    """
    try:
        await enqueue(job_id=job.id, document_id=document_id, idempotency_key=job.idempotency_key)
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


# --- deletion + retry (T-208, FR-ING-05/06, R-39) -----------------------------
#
# Same shape as the upload flow above: this layer owns the transaction, repositories
# flush, and typed errors reach the route which maps them to status codes.


async def _load_authorized(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user: User,
    is_admin: bool,
    for_update: bool = False,
) -> Document:
    """Load a document the caller is allowed to act on, or raise.

    R-39(1): owner **or** administrator. A document belonging to someone else raises
    `DocumentNotFoundError`, not a permission error — under OI-15's per-user scoping the
    caller has no legitimate way to know the id exists, and a distinguishable `403` would
    confirm it (NFR-SEC-02). Same reasoning as the foreign-conversation branch above.

    An already soft-deleted document is still returned: `DELETE` must report it as such
    (R-39(2)) rather than 404, and the caller inspects `status`.
    """
    repo = DocumentRepository(session)
    document = await (repo.get_for_update(document_id) if for_update else repo.get(document_id))
    if document is None or (not is_admin and document.owner_id != user.id):
        raise DocumentNotFoundError(str(document_id))
    return document


async def request_deletion(
    *,
    document_id: uuid.UUID,
    user: User,
    is_admin: bool,
    session: AsyncSession,
    queue: JobQueue,
) -> DeletionOutcome:
    """FR-ING-05's synchronous half: leave retrieval now, purge in the background.

    Everything before the commit is the part the user is waiting on, and it is deliberately
    tiny — a status write, a `searchable = false`, and a job row. The bytes, the vectors and
    the object-storage round-trip all belong to the worker.

    The row is loaded `FOR UPDATE` so this transaction and a concurrent ingest swap
    serialise on the document rather than on ordering luck (R-39(8)).
    """
    await _reject_if_processing(session, owner_id=user.id)

    document = await _load_authorized(
        session, document_id=document_id, user=user, is_admin=is_admin, for_update=True
    )
    if document.status is DocumentStatus.DELETED:
        # Idempotent by R-39(2): the row the client is looking at is genuinely gone, so a
        # second click is a `200`, not an error.
        return DeletionOutcome(
            document_id=document.id, job_id=None, status=document.status, already_deleted=True
        )

    jobs = KnowledgeJobRepository(session)

    # R-39(8), first of the three points: an ingestion that has not started must not start.
    # `workers/ingest.py` also short-circuits on the document's state, so this is belt and
    # braces — but it is what leaves a *diagnosable* job row behind rather than a job that
    # silently vanishes.
    for open_job in await jobs.list_open_for_document(document_id, job_type=JobType.INGEST):
        await jobs.update_status(
            open_job,
            JobStatus.FAILED,
            error_code=DOCUMENT_DELETED,
            error_message="superseded: the document was deleted",
        )

    await DocumentRepository(session).mark_delete_pending(document)

    # One deletion job per document, ever. The key is unique, so a repeat `DELETE` — the
    # re-drive for a dead-lettered purge — resets this row instead of minting a second.
    key = f"delete:{document_id}"
    job = await jobs.get_by_idempotency_key(key)
    if job is None:
        job = KnowledgeJob(
            document_id=document_id,
            job_type=JobType.DELETE,
            status=JobStatus.QUEUED,
            # Not a build target here; it records which version was live when the deletion
            # was requested, and keeps the NOT NULL column honest.
            document_version=document.current_version,
            idempotency_key=key,
        )
        await jobs.add(job)
    else:
        await jobs.requeue(job)

    await audit.record_document_event(
        session, actor_id=user.id, document_id=document_id, action="delete"
    )
    await session.commit()

    await _enqueue_quietly(queue.enqueue_delete, session=session, job=job, document_id=document_id)
    return DeletionOutcome(
        document_id=document_id,
        job_id=job.id,
        status=DocumentStatus.DELETE_PENDING,
        already_deleted=False,
    )


async def retry_ingestion(
    *,
    document_id: uuid.UUID,
    user: User,
    is_admin: bool,
    session: AsyncSession,
    queue: JobQueue,
) -> RetryOutcome:
    """FR-ING-06's retry: re-run ingestion for a terminally failed document (R-39(5))."""
    await _reject_if_processing(session, owner_id=user.id)

    document = await _load_authorized(
        session, document_id=document_id, user=user, is_admin=is_admin, for_update=True
    )
    if document.status is not DocumentStatus.FAILED:
        # `FAILED` is the only retryable state by construction: R-38(5) keeps a *retryable*
        # failure in its in-flight state, so anything else here is either still running —
        # where a second dispatch would race the worker — or already `ACTIVE`.
        raise NotRetryableError(f"document is {document.status}, not FAILED")

    jobs = KnowledgeJobRepository(session)
    version = await jobs.latest_ingest_version(document_id) or document.current_version
    attempt = await jobs.count_for_document_version(
        document_id, job_type=JobType.INGEST, document_version=version
    )

    # A **new** row, not a reset of the failed one: `idempotency_key` is unique, the failed
    # row is the diagnostic record `GET /jobs/{id}` renders and must survive, and a fresh
    # broker `_job_id` cannot collide with whatever arq still holds for the old key.
    job = KnowledgeJob(
        document_id=document_id,
        job_type=JobType.INGEST,
        status=JobStatus.QUEUED,
        document_version=version,
        idempotency_key=f"ingest:{document_id}:v{version}:r{attempt}",
    )
    await jobs.add(job)

    # Cleared, not left: a document back in the queue must stop showing the reason its last
    # attempt failed in the FR-KBM-04 surface.
    await DocumentRepository(session).set_status(document, DocumentStatus.QUEUED, clear_error=True)
    await session.commit()

    await _enqueue_quietly(queue.enqueue_ingest, session=session, job=job, document_id=document_id)
    return RetryOutcome(document_id=document_id, job_id=job.id, status=DocumentStatus.QUEUED)


# --- replace (T-209, FR-KBM-07/08, R-36/R-40) ---------------------------------


#: R-40(2). Every other state is either in flight — where a replace would race the worker,
#: by exactly R-39(5)'s argument for retry — or on the deletion path, where the user has
#: already asked for the document to go away.
_REPLACEABLE_STATES = frozenset({DocumentStatus.ACTIVE, DocumentStatus.FAILED})


async def _supersede_open_ingests(jobs: KnowledgeJobRepository, document_id: uuid.UUID) -> None:
    """Fail any open INGEST job for an earlier version (R-40(4)'s companion).

    The `_REPLACEABLE_STATES` gate should make an open job impossible, but a sweeper
    re-dispatch or an arq redelivery can still produce one — and a stale job that runs
    after this transaction has repointed `storage_uri` would build the *new* bytes under
    the *old* version number.

    Same honest limitation as `request_deletion`'s identical loop: this cannot stop a job
    that is already `RUNNING`. What it buys is that "at most one open INGEST job per
    document" holds by construction, which is the invariant both the version allocation
    below and the swap guard in `workers/ingest.py` reason from; the swap guard is what
    catches the already-running case.
    """
    for open_job in await jobs.list_open_for_document(document_id, job_type=JobType.INGEST):
        await jobs.update_status(
            open_job,
            JobStatus.FAILED,
            error_code=SUPERSEDED,
            error_message="superseded: a newer version of the document was uploaded",
        )


async def replace_document(
    *,
    document_id: uuid.UUID,
    upload: UploadFile,
    user: User,
    is_admin: bool,
    session: AsyncSession,
    storage: ObjectStorage,
    queue: JobQueue,
    settings: Settings | None = None,
) -> ReplaceOutcome:
    """FR-KBM-07's Replace: new bytes for an existing document, at version n+1 (R-40).

    The whole point is that **the old version keeps answering questions throughout**. This
    function therefore writes the new original, repoints the document at it and queues the
    build — and touches none of `current_version`, `searchable`, `chunk_count` or
    `page_count`, which describe the version still serving. R-36(3)'s swap is the worker's
    job, and until it commits a retrieval query is answered exactly as it was before this
    request arrived.

    That guarantee rests on a property of `_access_predicates` worth naming: it filters
    `searchable` and `document_version = current_version`, and **not** `Document.status`.
    Setting the status to `QUEUED` here is therefore invisible to retrieval. A future
    `status == ACTIVE` predicate would silently break replace.
    """
    settings = settings or get_settings()
    filename = upload.filename or "upload"

    await _reject_if_processing(session, owner_id=user.id, settings=settings)

    # --- 1. authorize and gate *before* buffering 50 MB --------------------------------
    # Without this pre-check any authenticated caller can make the server hold a 50 MB body
    # — under the upload semaphore, blocking real uploads — against a document id they do
    # not own, only to be told `404` afterwards.
    document = await _load_authorized(
        session, document_id=document_id, user=user, is_admin=is_admin
    )
    if document.status not in _REPLACEABLE_STATES:
        raise NotReplaceableError(f"document is {document.status}, not ACTIVE or FAILED")
    owner_id = document.owner_id
    tenant_id = document.tenant_id
    knowledge_base_id = document.knowledge_base_id
    # Scalars are captured *before* this commit because committing expires the identity
    # map, and touching an attribute on the expired instance afterwards raises
    # `MissingGreenlet` under asyncio (the trap T-207 documents in `_handle_failure`).
    #
    # `commit()`, never `rollback()`: nothing was written, so this is free, and it releases
    # the read transaction SQLAlchemy opened on the `get` above rather than holding it
    # `idle in transaction` across the whole upload window. A `rollback()` would also
    # discard everything flushed on this session — which in tests is the fixture data,
    # since the harness shares one savepoint-joined session with the app.
    await session.commit()

    slots = await _acquire_slot(settings)
    async with slots:
        # --- 2. read, hash, sniff — no lock, no transaction, nothing to compensate ------
        read = await _read_and_hash(upload, settings)
        detected = detect_file_type(read.payload, filename=filename)

        documents = DocumentRepository(session)
        jobs = KnowledgeJobRepository(session)

        # --- 3. re-load FOR UPDATE and re-check ----------------------------------------
        # Minutes may have passed reading the body, and this is precisely the window in
        # which a `DELETE` arrives. This lock is the single serialisation point for
        # replace-vs-delete, replace-vs-replace, and replace-vs-the-ingest-swap-guard —
        # all three take it on the same row, which is what keeps that analysis finite.
        document = await _load_authorized(
            session, document_id=document_id, user=user, is_admin=is_admin, for_update=True
        )
        if document.status not in _REPLACEABLE_STATES:
            raise NotReplaceableError(f"document is {document.status}, not ACTIVE or FAILED")

        # --- 4. checksum, before anything reaches storage ------------------------------
        existing = await documents.find_by_checksum(
            knowledge_base_id=knowledge_base_id, checksum_sha256=read.checksum
        )
        if existing is not None and existing.id == document_id:
            # Identical bytes: nothing to rebuild and no version to burn (R-40(1), the
            # FR-KBM-08 argument). Release the lock immediately.
            await session.commit()
            return ReplaceOutcome(
                document_id=document_id,
                job_id=None,
                status=document.status,
                version=document.current_version,
                duplicate=True,
            )
        if existing is not None:
            raise DuplicateChecksumError(str(existing.id))

        # --- 5. quota ------------------------------------------------------------------
        if settings.upload.enforce_quota:
            # Credit the bytes being replaced. The plain upload check double-counts: it
            # adds the new original without subtracting the one it supersedes. Charged to
            # `owner_id`, never `user.id` — an admin replacing someone's document must
            # debit the owner's allowance, not their own.
            used = await documents.total_bytes_for_owner(owner_id)
            projected = max(0, used - (document.size_bytes or 0)) + read.size
            if projected > settings.upload.user_quota_bytes:
                raise QuotaExceededError(
                    f"{projected} bytes would exceed the "
                    f"{settings.upload.user_quota_bytes}-byte allowance"
                )

        # --- 6. target version ---------------------------------------------------------
        # `MAX(knowledge_jobs.document_version)`, not `current_version` + 1: a previous
        # replace that failed has already burned its number, and reusing it would collide
        # on the unique `ingest:{doc}:v{n}` key. Same read as `retry_ingestion`.
        version = (await jobs.latest_ingest_version(document_id) or document.current_version) + 1

        # --- 7. store, record, commit --------------------------------------------------
        new_key = original_key(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            version=version,
            filename=filename,
        )
        try:
            # The `put` runs while the row lock is held. Deliberate: the key embeds the
            # version, and a version computed without the lock can be wrong by the time the
            # object lands (S3 has no rename to recover with). What it blocks is
            # `request_deletion`, a sub-millisecond transaction — and blocking *that* for
            # the duration of a replace is correct, not a defect. Peak exposure is bounded
            # by the upload semaphore.
            stored = await storage.put(
                new_key,
                read.payload,
                content_type=detected.mime_type,
                max_bytes=settings.upload.max_file_bytes,
            )

            try:
                async with session.begin_nested():
                    document.storage_uri = stored.uri
                    document.checksum_sha256 = read.checksum
                    document.size_bytes = read.size
                    # R-40(7): the format may change, so the filename and mime travel with
                    # the bytes. `original_key` above already used the new suffix.
                    document.filename = filename
                    document.mime_type = detected.mime_type
                    await session.flush()
            except IntegrityError as exc:
                # The partial unique index has an UPDATE face nobody reasons about — step 4
                # can be overtaken by a concurrent replace of a *different* document with
                # the same new bytes. The savepoint is what keeps this a clean `409`
                # instead of a poisoned unit of work and a 500.
                raise DuplicateChecksumError(read.checksum) from exc

            # NOT current_version, NOT searchable, NOT chunk_count, NOT page_count — those
            # describe the version that is still answering questions (R-36(3)).
            await documents.set_status(document, DocumentStatus.QUEUED, clear_error=True)

            await _supersede_open_ingests(jobs, document_id)

            job = KnowledgeJob(
                document_id=document_id,
                job_type=JobType.INGEST,
                status=JobStatus.QUEUED,
                document_version=version,
                idempotency_key=f"ingest:{document_id}:v{version}",
            )
            await jobs.add(job)

            await audit.record_document_event(
                session, actor_id=user.id, document_id=document_id, action="replace"
            )
            await session.commit()
        except Exception:
            # One handler for the put, the update, the job insert, the audit and the
            # commit — the same shape as `_store_and_record`. Every failure path leaves the
            # old `storage_uri` and checksum intact, no v(n+1) object, no job row, and a
            # document still serving.
            await session.rollback()
            await _delete_quietly(storage, new_key)
            raise

    await _enqueue_quietly(queue.enqueue_ingest, session=session, job=job, document_id=document_id)
    return ReplaceOutcome(
        document_id=document_id,
        job_id=job.id,
        status=DocumentStatus.QUEUED,
        version=version,
        duplicate=False,
    )


__all__ = [
    "ConversationNotFoundError",
    "DeletionOutcome",
    "DocumentNotFoundError",
    "DuplicateChecksumError",
    "EmptyFileError",
    "FileTooLargeError",
    "MissingConversationError",
    "NotReplaceableError",
    "NotRetryableError",
    "ObjectTooLargeError",
    "QuotaExceededError",
    "ReplaceOutcome",
    "RetryOutcome",
    "UnsupportedFileTypeError",
    "UploadError",
    "UploadOutcome",
    "UploadScope",
    "replace_document",
    "request_deletion",
    "resolve_scope_kb_id",
    "retry_ingestion",
    "upload_document",
]
