"""``/documents`` routes — upload (T-202, FR-ING-02).

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

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.auth.dependencies import CurrentUser, DbSession
from app.db.enums import DocumentStatus
from app.security.content_validation import UnsupportedFileTypeError
from app.security.rate_limit import limiter, principal_or_ip_key, upload_limit
from app.services import documents as documents_service
from app.services.documents import (
    ConversationNotFoundError,
    EmptyFileError,
    FileTooLargeError,
    MissingConversationError,
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
