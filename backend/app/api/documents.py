"""``/documents`` routes — upload (T-202, FR-ING-02), delete and retry (T-208,
FR-ING-05/06), list/get/replace (T-209, FR-KBM-03/04/07/09, R-40).

Thin handlers: orchestration lives in `app.services.documents`, and this layer maps its
typed errors to status codes, matching `app.api.users`.

The route takes a **scope** rather than Source A §6's `/knowledge-bases/{id}/documents`
(ruling R-33): under R-25's two-scope mapping the GLOBAL knowledge base is created on
demand and the per-conversation one is implicit, so a client has no id to put in the path.
`scope` maps directly onto the two sections the FR-KBM-03 modal already shows.

All three FR-ERR rejection strings are provisional pending §8.4 — the limits themselves
(50 MB, 10 GB) are normative via R-11.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, assert_never

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    SettingsDep,
    StreamSessionmaker,
)
from app.db.enums import DocumentStatus, KBVisibility
from app.db.repositories.documents import DocumentListing, DocumentRepository
from app.security.content_validation import UnsupportedFileTypeError
from app.security.rate_limit import limiter, principal_or_ip_key, upload_limit
from app.services import document_events
from app.services import documents as documents_service
from app.services.document_events import DocumentState
from app.services.documents import (
    ConversationNotFoundError,
    DocumentNotFoundError,
    DuplicateChecksumError,
    EmptyFileError,
    FileTooLargeError,
    MissingConversationError,
    NotReplaceableError,
    NotRetryableError,
    ProcessingLockedError,
    QuotaExceededError,
    UploadScope,
)
from app.services.jobs import JobQueueDep
from app.services.object_storage import ObjectStorageDep, ObjectStorageError, ObjectTooLargeError

router = APIRouter(prefix="/documents", tags=["documents"])

_TOO_LARGE = "File is too large — the maximum upload size is 50 MB."  # TBD(§8.4) FR-ERR-01
_UNSUPPORTED = "Unsupported file type — upload a PDF, DOCX, CSV, or MD file."  # TBD(§8.4) FR-ERR-03
_QUOTA = (  # TBD(§8.4) FR-ERR-02
    "Storage limit reached — you have used your 10 GB allowance. Delete documents to free space."
)
_EMPTY = "The file is empty."
_NO_CONVERSATION = "A conversation_id is required for chat-scope uploads."
_CONVERSATION_NOT_FOUND = "Conversation not found."
_STORAGE_DOWN = "Storage service unavailable — please try again."
_DOCUMENT_NOT_FOUND = "Document not found."
_TOO_MANY_STREAMS = "Too many open document streams — close one and try again."  # TBD(§8.4) copy
_NOT_RETRYABLE = "Only a failed document can be retried."  # TBD(§8.4) copy
_NOT_REPLACEABLE = "Only an active or failed document can be replaced."  # TBD(§8.4) copy
_DUPLICATE_CHECKSUM = (  # TBD(§8.4) copy
    "That file is already in your knowledge base as a different document."
)
_PROCESSING_LOCKED = (  # TBD(§8.4) copy — FR-STA-02 / FR-ORC-04, R-43
    "Knowledge-base actions are paused while a response is being generated. "
    "Try again once the answer finishes."
)

#: FR-STA-02's four gated verbs answer `409`, not `423` or `429`. `429` is disqualified
#: outright — these routes already carry `@limiter.limit`, so the client could not tell a
#: throttle from a busy chat. `423 Locked` describes a locked *resource*, and what is locked
#: here is the caller's session, not the document. `409` is the vocabulary this surface
#: already uses for `NotRetryableError` / `NotReplaceableError`: one status, one handler.
_PROCESSING_LOCKED_RESPONSE = {
    status.HTTP_409_CONFLICT: {"description": "A response is generating for this user."},
}


class UploadResponse(BaseModel):
    """FR-ING-02's `202` body, plus the FR-KBM-08 duplicate signal.

    On a duplicate the response is `200` (nothing was queued), `job_id` is null and
    `status` is the *existing* document's lifecycle state, so the drop zone can point at
    the row already present instead of showing an error.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: DocumentStatus
    duplicate: bool = False


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {"description": "Duplicate checksum — not re-ingested."},
        status.HTTP_202_ACCEPTED: {"description": "Accepted; ingestion queued."},
        **_PROCESSING_LOCKED_RESPONSE,
    },
    summary="Upload a document",
)
@limiter.limit(upload_limit, key_func=principal_or_ip_key)
async def upload_document(
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    storage: ObjectStorageDep,
    queue: JobQueueDep,
    file: Annotated[UploadFile, File()],
    scope: Annotated[UploadScope, Form()] = "global",
    conversation_id: Annotated[uuid.UUID | None, Form()] = None,
) -> UploadResponse:
    try:
        outcome = await documents_service.upload_document(
            upload=file,
            scope=scope,
            conversation_id=conversation_id,
            user=user,
            session=session,
            storage=storage,
            queue=queue,
        )
    except ProcessingLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _PROCESSING_LOCKED) from exc
    except (FileTooLargeError, ObjectTooLargeError) as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, _TOO_LARGE) from exc
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, _UNSUPPORTED) from exc
    except QuotaExceededError as exc:
        # 507 (RFC 4918) is the precise "cannot store the representation" semantic, and
        # keeps the per-user quota distinguishable from the per-file 413 without the
        # client having to parse copy.
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, _QUOTA) from exc
    except EmptyFileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _EMPTY) from exc
    except MissingConversationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_CONVERSATION) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _CONVERSATION_NOT_FOUND) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN) from exc

    if outcome.duplicate:
        response.status_code = status.HTTP_200_OK
    return UploadResponse(
        document_id=outcome.document_id,
        job_id=outcome.job_id,
        status=outcome.status,
        duplicate=outcome.duplicate,
    )


# --- deletion + retry (T-208, FR-ING-05/06, R-39) -----------------------------


class DeleteResponse(BaseModel):
    """FR-ING-05's `202` body, plus the R-39(2) already-deleted signal.

    `already_deleted` pairs with a `200`: nothing was queued, so `job_id` is null. Same
    shape as the FR-KBM-08 duplicate, and for the same reason — a second Delete click on a
    row that has already gone is not an error the GUI should render.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: DocumentStatus
    already_deleted: bool = False


class RetryResponse(BaseModel):
    """FR-ING-06's retry `202`. Always a fresh job id (R-39(5))."""

    document_id: uuid.UUID
    job_id: uuid.UUID
    status: DocumentStatus


@router.delete(
    "/{document_id}",
    response_model=DeleteResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {"description": "Already deleted — nothing queued."},
        status.HTTP_202_ACCEPTED: {"description": "Accepted; the document left retrieval."},
        status.HTTP_404_NOT_FOUND: {"description": "No such document for this caller."},
        **_PROCESSING_LOCKED_RESPONSE,
    },
    summary="Delete a document",
)
@limiter.limit(upload_limit, key_func=principal_or_ip_key)
async def delete_document(
    request: Request,
    response: Response,
    document_id: uuid.UUID,
    user: CurrentUser,
    principal: CurrentPrincipal,
    session: DbSession,
    queue: JobQueueDep,
) -> DeleteResponse:
    try:
        outcome = await documents_service.request_deletion(
            document_id=document_id,
            user=user,
            is_admin=principal.is_administrator,
            session=session,
            queue=queue,
        )
    except ProcessingLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _PROCESSING_LOCKED) from exc
    except DocumentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _DOCUMENT_NOT_FOUND) from exc

    if outcome.already_deleted:
        response.status_code = status.HTTP_200_OK
    return DeleteResponse(
        document_id=outcome.document_id,
        job_id=outcome.job_id,
        status=outcome.status,
        already_deleted=outcome.already_deleted,
    )


@router.post(
    "/{document_id}/retry",
    response_model=RetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {"description": "Accepted; ingestion re-queued."},
        status.HTTP_404_NOT_FOUND: {"description": "No such document for this caller."},
        status.HTTP_409_CONFLICT: {
            "description": "The document is not in FAILED, or a response is generating."
        },
    },
    summary="Retry a failed ingestion",
)
@limiter.limit(upload_limit, key_func=principal_or_ip_key)
async def retry_document(
    request: Request,
    # Unused by the handler, but slowapi writes its `X-RateLimit-*` headers onto it and
    # raises if the endpoint does not declare one.
    response: Response,
    document_id: uuid.UUID,
    user: CurrentUser,
    principal: CurrentPrincipal,
    session: DbSession,
    queue: JobQueueDep,
) -> RetryResponse:
    try:
        outcome = await documents_service.retry_ingestion(
            document_id=document_id,
            user=user,
            is_admin=principal.is_administrator,
            session=session,
            queue=queue,
        )
    except ProcessingLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _PROCESSING_LOCKED) from exc
    except DocumentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _DOCUMENT_NOT_FOUND) from exc
    except NotRetryableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_RETRYABLE) from exc

    return RetryResponse(
        document_id=outcome.document_id, job_id=outcome.job_id, status=outcome.status
    )


# --- list + get + replace (T-209, FR-KBM-03/04/07/09, R-40) --------------------


class DocumentResponse(BaseModel):
    """One document, for both the list and the single-document read (R-40(5)).

    **Metadata only.** R-31(4) names T-209 as a likely route to a download/export/preview
    surface, which would make Corpus a file-distribution vector and turn the malware
    scanner from optional into required — so `storage_uri` is deliberately absent and no
    field on this model carries or points at bytes. `checksum_sha256` is absent too: no
    requirement asks for it, and the duplicate story is already told by the upload and
    replace responses. No chunk id appears anywhere either (R-36(6)(b)): a replaced
    document's historical chunk ids dangle by design, and nothing here may become a way to
    resolve one.

    **`current_version` is the version serving retrieval, not the version being built.**
    While a replace is in flight `latest_job_document_version` is `current_version + 1`,
    and that inequality is the only signal distinguishing "this document failed and is
    silent" from "this document's replace failed and the previous version still answers"
    (OI-29). The job's own status and progress are deliberately not denormalised here —
    `GET /jobs/{id}` owns them, and a surface rendering two statuses read at two different
    moments will eventually render them disagreeing.
    """

    document_id: uuid.UUID
    filename: str
    mime_type: str | None
    status: DocumentStatus
    current_version: int
    searchable: bool
    size_bytes: int | None
    page_count: int | None
    chunk_count: int | None
    error_message: str | None
    knowledge_base_id: uuid.UUID
    scope: UploadScope
    conversation_id: uuid.UUID | None
    latest_job_id: uuid.UUID | None
    latest_job_error_code: str | None
    latest_job_document_version: int | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ReplaceResponse(BaseModel):
    """R-40(1)'s `202`, plus the identical-bytes `200`.

    `version` is the version the worker will **build**; `DocumentResponse.current_version`
    stays on the one still serving until the swap commits (R-36(3)). The names differ on
    purpose. On the duplicate `200` the two are equal and `job_id` is null.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: DocumentStatus
    version: int
    duplicate: bool = False


def _to_response(listing: DocumentListing) -> DocumentResponse:
    """Build the DTO field by field.

    Never `model_validate(document, from_attributes=True)`: that idiom would pick up
    `storage_uri` and `checksum_sha256` the moment someone adds them to the model's field
    list, which is exactly how a metadata-only surface stops being one.
    """
    document = listing.document
    return DocumentResponse(
        document_id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        status=document.status,
        current_version=document.current_version,
        searchable=document.searchable,
        size_bytes=document.size_bytes,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        error_message=document.error_message,
        knowledge_base_id=document.knowledge_base_id,
        scope="global" if listing.scope is KBVisibility.GLOBAL else "chat",
        conversation_id=listing.conversation_id,
        latest_job_id=listing.latest_job_id,
        latest_job_error_code=listing.latest_job_error_code,
        latest_job_document_version=listing.latest_job_document_version,
        created_at=document.created_at,
        updated_at=document.updated_at,
        deleted_at=document.deleted_at,
    )


@router.get(
    "",
    response_model=list[DocumentResponse],
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "scope=chat without a conversation_id."},
        status.HTTP_404_NOT_FOUND: {"description": "No such conversation for this caller."},
    },
    summary="List documents",
)
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    scope: Annotated[UploadScope | None, Query()] = None,
    conversation_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentResponse]:
    """The FR-KBM-03/09 page for the calling user (R-40(5)).

    `scope`/`conversation_id` are spelled exactly as the upload form spells them, so the
    modal uses one vocabulary in both directions; omitting `scope` returns both sections,
    which is the FR-KBM-09 table view. A legitimate scope with nothing in it yet is an
    empty list, not a `404`.

    **Caller-scoped, with no admin widening** — deliberately asymmetric with `GET /{id}`,
    which is owner-or-admin so an admin can read anything they can act on. There is no GUI
    surface for browsing another user's knowledge base (FR-KBM-01..09 is the caller's own
    modal), so a cross-user list would be speculative surface needing its own authorization
    story.

    Not rate-limited: no read route in this API is, and a limiter would force a
    `request`/`response` pair onto a pure read for nothing.
    """
    knowledge_base_id: uuid.UUID | None = None
    if scope is not None:
        try:
            knowledge_base_id = await documents_service.resolve_scope_kb_id(
                session, user=user, scope=scope, conversation_id=conversation_id
            )
        except MissingConversationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_CONVERSATION) from exc
        except ConversationNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _CONVERSATION_NOT_FOUND) from exc
        if knowledge_base_id is None:
            return []

    listings = await DocumentRepository(session).list_for_owner(
        owner_id=user.id,
        knowledge_base_id=knowledge_base_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return [_to_response(listing) for listing in listings]


# --- live status channel (T-210, FR-KBM-09, R-41) -----------------------------


class DocumentEventResponse(DocumentResponse):
    """The list/get DTO plus the one field the live channel adds (R-41(4)/(5)).

    Subclassed rather than redefined so the stream cannot drift from the route it mirrors:
    a field added to `DocumentResponse` appears here automatically, and a live table whose
    contents depended on whether the modal happened to be open when a change landed is
    exactly the failure this inheritance rules out.

    `stalled` is **derived, not stored** — no `DocumentStatus` carries it (R-38(3): a state
    exists to be written by a transition, and nothing transitions into "stalled") and
    FR-KBM-04 gains no ninth label. It means "an in-flight document has gone quiet longer
    than a worker could legitimately be silent", which T-212 measured as up to
    `job_timeout + 10` seconds behind arq's in-progress guard. It is **never** true for
    `DELETE_PENDING`/`DELETING`; R-39(7) requires those keep rendering as `Deleting`.
    """

    stalled: bool


def _to_event_response(state: DocumentState) -> DocumentEventResponse:
    return DocumentEventResponse(
        **_to_response(state.listing).model_dump(),
        stalled=state.stalled,
    )


@router.get(
    "/events",
    summary="Live document-status stream (SSE)",
    response_class=EventSourceResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "An SSE stream. `snapshot` carries the full set on connect, `document` one "
                "changed row, `removed` a document id that has left the set."
            ),
            "content": {"text/event-stream": {}},
        },
        status.HTTP_400_BAD_REQUEST: {"description": "scope=chat without a conversation_id."},
        status.HTTP_404_NOT_FOUND: {"description": "No such conversation for this caller."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many concurrent streams."},
    },
)
async def stream_documents(
    user: CurrentUser,
    session: DbSession,
    sessionmaker: StreamSessionmaker,
    settings: SettingsDep,
    scope: Annotated[UploadScope | None, Query()] = None,
    conversation_id: Annotated[uuid.UUID | None, Query()] = None,
) -> EventSourceResponse:
    """FR-KBM-09's live surface (R-41).

    Filters, scope resolution and authorization are deliberately identical to
    `list_documents` — same parameter spelling, same `400`/`404`, same caller scoping with
    no admin widening — so the stream and the page it updates can never disagree about
    what the caller may see.

    **Authenticated by the ordinary `Authorization` header (R-41(3)).** A browser's
    `EventSource` cannot send one, so the GUI (T-508) consumes this with `fetch` +
    `ReadableStream` and parses the frames itself. Every query-string alternative was
    rejected: a raw token there is written to access logs, proxy logs, `Referer` headers
    and browser history, and stays valid in all of them long after the tab closes.

    The `session` dependency resolves the scope **once, before streaming starts**, so a bad
    request still fails as an ordinary `400`/`404` — once the `200` and the first byte are
    out, there is no status code left to fail with. The loop itself uses `sessionmaker`.
    """
    knowledge_base_id: uuid.UUID | None = None
    if scope is not None:
        try:
            knowledge_base_id = await documents_service.resolve_scope_kb_id(
                session, user=user, scope=scope, conversation_id=conversation_id
            )
        except MissingConversationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_CONVERSATION) from exc
        except ConversationNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _CONVERSATION_NOT_FOUND) from exc
        # A scope that resolves to no knowledge base yet (nothing uploaded to it) still
        # gets a stream: the KB is created on demand by the first upload, and a client that
        # opened the modal a moment early must not be left without the events describing it.

    try:
        document_events.registry.acquire(user.id, limit=settings.sse.max_streams_per_user)
    except document_events.TooManyStreamsError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY_STREAMS) from exc

    async def publish() -> AsyncIterator[dict[str, str]]:
        try:
            events = document_events.stream_document_events(
                sessionmaker,
                owner_id=user.id,
                knowledge_base_id=knowledge_base_id,
                poll_interval=settings.sse.poll_interval_seconds,
                stall_after=settings.stall_after,
            )
            async for event in events:
                yield _frame(event)
        finally:
            # `finally`, not after the loop: the normal end of an SSE stream is the client
            # vanishing, which reaches this generator as `GeneratorExit`/`CancelledError`.
            # Releasing only on a clean exit would leak a slot per closed tab until the
            # user hit the cap and could open no more.
            document_events.registry.release(user.id)

    return EventSourceResponse(publish(), ping=int(settings.sse.ping_seconds))


def _frame(event: document_events.DocumentEvent) -> dict[str, str]:
    """One SSE frame. `data` is JSON in every case, so the client has one parse path."""
    match event:
        case document_events.Snapshot(documents=documents):
            payload = [_to_event_response(state).model_dump(mode="json") for state in documents]
            return {"event": "snapshot", "data": json.dumps(payload)}
        case document_events.DocumentChanged(state=state):
            return {
                "event": "document",
                "data": _to_event_response(state).model_dump_json(),
            }
        case document_events.DocumentRemoved(document_id=document_id):
            return {"event": "removed", "data": json.dumps({"document_id": str(document_id)})}
        case _:  # pragma: no cover — exhaustive over the union; keeps mypy honest if it grows
            assert_never(event)


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such document for this caller."}},
    summary="Get one document",
)
async def get_document(
    document_id: uuid.UUID,
    user: CurrentUser,
    principal: CurrentPrincipal,
    session: DbSession,
) -> DocumentResponse:
    """One document's metadata, owner-or-admin (R-40(5)).

    A soft-deleted document is returned with its terminal state rather than 404'd: a client
    that has just received `DELETE`'s `202` and polls must see `DELETED`, not a sudden
    `404` it cannot tell apart from a wrong id. The list excludes tombstones; this does not.
    """
    listing = await DocumentRepository(session).get_listing_scoped(
        document_id, owner_id=None if principal.is_administrator else user.id
    )
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _DOCUMENT_NOT_FOUND)
    return _to_response(listing)


@router.post(
    "/{document_id}/replace",
    response_model=ReplaceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {"description": "Identical bytes — nothing queued."},
        status.HTTP_202_ACCEPTED: {"description": "Accepted; the new version is queued."},
        status.HTTP_404_NOT_FOUND: {"description": "No such document for this caller."},
        status.HTTP_409_CONFLICT: {
            "description": (
                "Not ACTIVE/FAILED, the bytes belong to another document, "
                "or a response is generating."
            )
        },
    },
    summary="Replace a document with a new version",
)
@limiter.limit(upload_limit, key_func=principal_or_ip_key)
async def replace_document(
    request: Request,
    response: Response,
    document_id: uuid.UUID,
    user: CurrentUser,
    principal: CurrentPrincipal,
    session: DbSession,
    storage: ObjectStorageDep,
    queue: JobQueueDep,
    file: Annotated[UploadFile, File()],
) -> ReplaceResponse:
    """FR-KBM-07's Replace (R-40(1)): new bytes at version n+1, old version keeps serving."""
    try:
        outcome = await documents_service.replace_document(
            document_id=document_id,
            upload=file,
            user=user,
            is_admin=principal.is_administrator,
            session=session,
            storage=storage,
            queue=queue,
        )
    except ProcessingLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _PROCESSING_LOCKED) from exc
    except DocumentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _DOCUMENT_NOT_FOUND) from exc
    except NotReplaceableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _NOT_REPLACEABLE) from exc
    except DuplicateChecksumError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_CHECKSUM) from exc
    except (FileTooLargeError, ObjectTooLargeError) as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, _TOO_LARGE) from exc
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, _UNSUPPORTED) from exc
    except QuotaExceededError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, _QUOTA) from exc
    except EmptyFileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _EMPTY) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN) from exc

    if outcome.duplicate:
        response.status_code = status.HTTP_200_OK
    return ReplaceResponse(
        document_id=outcome.document_id,
        job_id=outcome.job_id,
        status=outcome.status,
        version=outcome.version,
        duplicate=outcome.duplicate,
    )
