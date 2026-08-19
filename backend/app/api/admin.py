"""``/admin`` routes — the FR-ING-03 re-embed trigger (T-608, R-84).

**The first `/admin` surface in the API, and it is deliberately a small one.** §4 describes no
administrator screen, so nothing here is rendered by the GUI; these two routes exist because
R-84(5) chose an authenticated administrator over a database-credentials-only tool for the one
operation that spends money on someone else's corpus. `tools.reembed` calls the same two
service functions, so the CLI and the route cannot disagree about what is stale or about what a
rebuild does — the §8.53(5) lesson, where a second entry point inherits a pipeline instead of
copying it.

**The pair is a read plus a per-document write, never a batch verb.** A `POST /reembed?limit=50`
would hold one HTTP request open across fifty 50 MB object copies, and the loop that decides
*which* fifty is exactly what a client (the CLI today, an admin screen later) should own. So:
`GET .../stale` enumerates and prices the work, `POST .../{id}/reembed` does one unit of it.

Both are `RequireAdmin`. R-39(1) already gives an administrator owner-or-admin power over every
document, so this widens *who may act on a document* not at all — it adds a verb, not a reach.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.api.errors import (
    RATE_LIMITED,
    RebuildConflictDetail,
    RebuildConflictResponse,
    error_responses,
)
from app.auth.dependencies import CurrentUser, DbSession, RequireAdmin, SettingsDep
from app.db.enums import DocumentStatus
from app.security.rate_limit import UPLOAD_BUCKET, limiter, principal_or_ip_key, upload_limit
from app.services import documents as documents_service
from app.services import reembed
from app.services.documents import (
    DocumentNotFoundError,
    NotRebuildableError,
    NotStaleError,
    OriginalCorruptError,
)
from app.services.jobs import JobQueueDep
from app.services.object_storage import ObjectStorageDep, ObjectStorageError

router = APIRouter(prefix="/admin", tags=["admin"])

_DOCUMENT_NOT_FOUND = "Document not found."
_STORAGE_DOWN = "Storage service unavailable — please try again."
_NOT_REBUILDABLE = "Only an ACTIVE document can be re-embedded."  # TBD(§8.4)
_NOT_STALE = "This document was already built by the configured pipeline."  # TBD(§8.4)
_ORIGINAL_CORRUPT = (  # TBD(§8.4)
    "The stored original does not match its recorded checksum — re-upload the document."
)

_CONFLICT_COPY = {
    "NOT_REBUILDABLE": _NOT_REBUILDABLE,
    "NOT_STALE": _NOT_STALE,
    "ORIGINAL_CORRUPT": _ORIGINAL_CORRUPT,
}


def _conflict(code: str) -> Any:
    """Build a `409` detail from the published model, never a dict literal.

    R-57's rule, and `processing_locked()`'s shape: a model that merely *describes* a hand-built
    dict drifts the first time someone edits the literal, and the drift is invisible because the
    schema still validates. Returns the *detail*, so the status stays visible at the raise site.
    """
    return RebuildConflictDetail(error_code=code, message=_CONFLICT_COPY[code]).model_dump()  # type: ignore[arg-type]


class ConfiguredPipelineResponse(BaseModel):
    """The three non-text inputs to FR-ING-03's fingerprint, as configured right now."""

    embedding_model: str
    chunking_version: str
    preprocessing_version: str


class StaleDocumentResponse(BaseModel):
    """One document whose live version was built by a different pipeline.

    Metadata only (R-40(5)): no chunk text, no `storage_uri`, no chunk id — which is also what
    keeps R-31(4)'s revisit trigger untripped, since nothing here re-serves uploaded content.
    """

    document_id: uuid.UUID
    owner_id: uuid.UUID
    filename: str
    document_version: int
    chunk_count: int
    token_count: int
    drifted_inputs: list[str]


class StaleTotalsResponse(BaseModel):
    """Totals over the whole stale set, not the page.

    `token_count` is FR-ING-03's `ceil(len/4)` estimate (R-35(7)) summed over every chunk a full
    run would re-embed — indicative rather than a quotation, and here because "re-embed 4,100
    documents" and "spend ~11M embedding tokens" are the same sentence to the database and very
    different sentences to whoever is paying.

    `in_flight` is reported separately rather than folded in: a document already being rebuilt is
    neither work to do nor work done, and omitting it would read as "nothing left" mid-run.
    """

    documents: int
    chunks: int
    token_count: int
    in_flight: int


class StaleDocumentsResponse(BaseModel):
    """What `GET /admin/documents/stale` renders."""

    pipeline: ConfiguredPipelineResponse
    totals: StaleTotalsResponse
    documents: list[StaleDocumentResponse]


class RebuildResponse(BaseModel):
    """What `POST /admin/documents/{id}/reembed` renders.

    Both versions, because both are true at once: `version` is what the worker will build,
    `previous_version` what keeps answering questions until the swap commits (R-36(3)). One
    number for both would be the likeliest source of a bug on this surface — the reason
    `ReplaceOutcome` makes the same distinction.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID
    status: DocumentStatus
    version: int
    previous_version: int


@router.get(
    "/documents/stale",
    response_model=StaleDocumentsResponse,
    responses={**RATE_LIMITED},
    summary="List documents whose embeddings predate the configured pipeline",
)
async def list_stale_documents(
    admin: RequireAdmin,
    session: DbSession,
    settings: SettingsDep,
    owner_id: Annotated[
        uuid.UUID | None, Query(description="Restrict to one owner's documents.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=reembed.MAX_PAGE)] = reembed.DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StaleDocumentsResponse:
    """Read-only, and safe to run against production at any moment.

    Declares no `409` and no `503`: it takes no lock, touches no object store and has no state to
    conflict with. R-77(2) — a route may not advertise a status it cannot return.
    """
    plan = await reembed.plan_reembed(
        session, owner_id=owner_id, limit=limit, offset=offset, settings=settings
    )
    return StaleDocumentsResponse(
        pipeline=ConfiguredPipelineResponse(
            embedding_model=plan.pipeline.embedding_model,
            chunking_version=plan.pipeline.chunking_version,
            preprocessing_version=plan.pipeline.preprocessing_version,
        ),
        totals=StaleTotalsResponse(
            documents=plan.totals.documents,
            chunks=plan.totals.chunks,
            token_count=plan.totals.token_count,
            in_flight=plan.totals.in_flight,
        ),
        documents=[
            StaleDocumentResponse(
                document_id=row.document_id,
                owner_id=row.owner_id,
                filename=row.filename,
                document_version=row.document_version,
                chunk_count=row.chunk_count,
                token_count=row.token_count,
                drifted_inputs=list(row.drifted_inputs),
            )
            for row in plan.documents
        ],
    )


@router.post(
    "/documents/{document_id}/reembed",
    response_model=RebuildResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {"description": "Accepted; the rebuild is queued."},
        **error_responses(
            (404, "No such document."),
            (503, "Object storage is unreachable."),
        ),
        status.HTTP_409_CONFLICT: {
            "model": RebuildConflictResponse,
            "description": "Not ACTIVE, already current, or the stored original is corrupt.",
        },
        **RATE_LIMITED,
    },
    summary="Re-embed one document under the configured pipeline",
)
# `shared_limit` with an explicit scope, never a plain `@limiter.limit`: slowapi's default
# `key_style="url"` keys the bucket on the *concrete path*, so a route with an id in it hands a
# caller one fresh allowance per document — which is precisely R-77(3)'s measured defect on the
# upload bucket. Sharing that bucket is also right on the merits: a rebuild costs the same object
# copy and the same embedding calls an upload does.
@limiter.shared_limit(upload_limit, scope=UPLOAD_BUCKET, key_func=principal_or_ip_key)
async def reembed_document(
    request: Request,
    # Unused by the handler, but slowapi writes its `X-RateLimit-*` headers onto it and raises if
    # the endpoint does not declare one.
    response: Response,
    document_id: uuid.UUID,
    admin: RequireAdmin,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    storage: ObjectStorageDep,
    queue: JobQueueDep,
) -> RebuildResponse:
    try:
        outcome = await documents_service.rebuild_document(
            document_id=document_id,
            # The administrator's own `users.id`, which is what `audit_log.actor_id` references —
            # `RequireAdmin` yields the token's `Principal` and gates the route, while the mirrored
            # user row is what the foreign key needs. `tools.reembed` passes `None` instead, which
            # the column already allows.
            actor_id=user.id,
            session=session,
            storage=storage,
            queue=queue,
            settings=settings,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _DOCUMENT_NOT_FOUND) from exc
    except NotRebuildableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _conflict("NOT_REBUILDABLE")) from exc
    except NotStaleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _conflict("NOT_STALE")) from exc
    except OriginalCorruptError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, _conflict("ORIGINAL_CORRUPT")) from exc
    except ObjectStorageError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN) from exc

    return RebuildResponse(
        document_id=outcome.document_id,
        job_id=outcome.job_id,
        status=outcome.status,
        version=outcome.version,
        previous_version=outcome.previous_version,
    )


__all__ = ["router"]
