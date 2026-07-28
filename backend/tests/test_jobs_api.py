"""`GET /api/v1/jobs/{id}` tests (T-208, FR-ING-06, R-39(6)).

The endpoint is small; the parts worth testing are the scoping (a foreign job must be
indistinguishable from a missing one) and that it renders the retry diagnostics R-38(5)
deliberately keeps *off* the document — `attempt_count`, `error_code`, `error_message` are
the only place a user can see why an ingestion is on its third try.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.usefixtures("patch_jwks")


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _job(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    job_type: JobType = JobType.INGEST,
    status: JobStatus = JobStatus.RUNNING,
    attempts: int = 2,
    error_code: str | None = "OBJECT_STORAGE_UNAVAILABLE",
) -> KnowledgeJob:
    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        storage_uri="file:///x",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=16,
        status=DocumentStatus.EMBEDDING,
        current_version=1,
        searchable=False,
    )
    session.add(document)
    await session.flush()

    job = KnowledgeJob(
        document_id=document.id,
        job_type=job_type,
        status=status,
        progress=65,
        attempt_count=attempts,
        document_version=1,
        error_code=error_code,
        error_message="RateLimitError: slow down" if error_code else None,
        idempotency_key=f"{job_type.value.lower()}:{document.id}:v1",
    )
    session.add(job)
    await session.flush()
    return job


async def test_the_owner_sees_the_full_retry_state(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    owner_id, headers = await _caller(session, make_token)
    job = await _job(session, owner_id=owner_id)

    body = (await client.get(f"/api/v1/jobs/{job.id}", headers=headers)).json()

    assert body["job_id"] == str(job.id)
    assert body["document_id"] == str(job.document_id)
    assert body["job_type"] == JobType.INGEST
    assert body["status"] == JobStatus.RUNNING
    assert body["progress"] == 65
    # The diagnostics R-38(5) keeps off `documents.status` — this is their only surface.
    assert body["attempt_count"] == 2
    assert body["error_code"] == "OBJECT_STORAGE_UNAVAILABLE"
    assert body["error_message"] == "RateLimitError: slow down"
    assert body["document_version"] == 1
    assert body["created_at"] is not None
    assert body["completed_at"] is None


async def test_a_deletion_job_is_reported_too(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """FR-ING-06 covers both kinds: "ingestion/deletion progress is queryable"."""
    owner_id, headers = await _caller(session, make_token)
    job = await _job(session, owner_id=owner_id, job_type=JobType.DELETE, error_code=None)

    body = (await client.get(f"/api/v1/jobs/{job.id}", headers=headers)).json()

    assert body["job_type"] == JobType.DELETE
    assert body["error_code"] is None


async def test_a_foreign_job_is_404(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """NFR-SEC-02: not 403, which would confirm the id exists."""
    owner_id, _ = await _caller(session, make_token)
    _, intruder_headers = await _caller(session, make_token)
    job = await _job(session, owner_id=owner_id)

    assert (await client.get(f"/api/v1/jobs/{job.id}", headers=intruder_headers)).status_code == 404


async def test_an_admin_sees_any_job(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    owner_id, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    job = await _job(session, owner_id=owner_id)

    response = await client.get(f"/api/v1/jobs/{job.id}", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["job_id"] == str(job.id)


async def test_an_unknown_job_is_404(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    _, headers = await _caller(session, make_token)

    assert (await client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=headers)).status_code == 404


async def test_the_endpoint_requires_authentication(
    client: httpx.AsyncClient, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    owner_id, _ = await _caller(session, make_token)
    job = await _job(session, owner_id=owner_id)

    assert (await client.get(f"/api/v1/jobs/{job.id}")).status_code == 401
