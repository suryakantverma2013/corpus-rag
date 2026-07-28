"""``/jobs`` routes — ingestion/deletion job status (T-208, FR-ING-06, R-39(6)).

A read-only view of `knowledge_jobs`, which is where the whole retry story lives: R-38(5)
keeps a retryable failure *off* the document (`FAILED` means terminally failed), so
`attempt_count`, `error_code` and `error_message` here are the only place a user or an
operator can see why an ingestion is taking three tries. That is exactly what §11's
"preserve job diagnostics and retry state" asks for.

Deliberately **not** arq's result store: `keep_result = 0` discards it, and a Redis-resident
result would outlive nothing. The job row is the record.

Thin, like the other read routes — the scoping is a repository predicate, not logic here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from app.db.enums import JobStatus, JobType
from app.db.repositories.jobs import KnowledgeJobRepository

router = APIRouter(prefix="/jobs", tags=["jobs"])

_NOT_FOUND = "Job not found."


class JobResponse(BaseModel):
    """FR-ING-06's job view.

    `progress` is the coarse stage milestone the worker writes (5/25/45/65/85/100); no stage
    reports partial progress, so it moves in steps rather than smoothly.
    """

    job_id: uuid.UUID
    document_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    progress: int
    attempt_count: int
    document_version: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such job for this caller."}},
    summary="Get ingestion/deletion job status",
)
async def get_job(
    job_id: uuid.UUID,
    user: CurrentUser,
    principal: CurrentPrincipal,
    session: DbSession,
) -> JobResponse:
    # Administrators see any job (FR-USR-04); everyone else is scoped to jobs whose document
    # they own, and a foreign job is reported as missing rather than forbidden so the
    # response cannot confirm the id exists (NFR-SEC-02, R-39(6)).
    job = await KnowledgeJobRepository(session).get_scoped(
        job_id, owner_id=None if principal.is_administrator else user.id
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    return JobResponse(
        job_id=job.id,
        document_id=job.document_id,
        job_type=job.job_type,
        status=job.status,
        progress=job.progress,
        attempt_count=job.attempt_count,
        document_version=job.document_version,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )
