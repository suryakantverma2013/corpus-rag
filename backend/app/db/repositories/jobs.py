"""Knowledge-job repository (T-102). Idempotency + retry state (FR-ING-04/06)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.enums import JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.base import BaseRepository

# Sentinel written by T-202 when the post-commit enqueue fails: the job row is committed
# and the API still returns 202, so this is real work nobody dispatched. The T-207 sweeper
# is what eventually dispatches it.
ENQUEUE_FAILED = "ENQUEUE_FAILED"

# Written when an ingestion is abandoned because the document entered the FR-ING-05
# deletion path. Lives here rather than in either writer because **both** write it: the
# delete request supersedes in-flight ingest jobs, and the ingest task itself short-circuits
# on a document already being deleted (R-39(8)). Two spellings would make §11's
# "delete while ingestion is running" untraceable in the job table.
DOCUMENT_DELETED = "DOCUMENT_DELETED"

# Written when a replace (T-209, R-40) abandons an ingest job for an *earlier* version of
# the same document. R-40(2)'s state gate should make an open job impossible, but a sweeper
# re-dispatch or an arq redelivery can still produce one — and a stale job that runs after
# the replace has repointed `storage_uri` would build the new bytes under the old version
# number. Marking them keeps "at most one open INGEST job per document" true by
# construction, which is the invariant both the version allocation and the swap guard rest
# on. Honest limitation, shared with the deletion path: this cannot stop a job already
# RUNNING — the swap guard is what catches that one.
SUPERSEDED = "SUPERSEDED"


class KnowledgeJobRepository(BaseRepository[KnowledgeJob]):
    model = KnowledgeJob

    async def get_by_idempotency_key(self, idempotency_key: str) -> KnowledgeJob | None:
        stmt = select(KnowledgeJob).where(KnowledgeJob.idempotency_key == idempotency_key)
        return (await self.session.scalars(stmt)).first()

    async def list_undispatched(
        self, *, older_than: datetime, limit: int = 100
    ) -> Sequence[KnowledgeJob]:
        """Ingestion jobs T-202 committed but could not enqueue (T-207 sweeper).

        `older_than` keeps the sweeper off rows a live request is still racing to
        dispatch — `_enqueue_quietly` writes the sentinel and commits it, so a job seen
        here a millisecond later may already be on the broker. Re-enqueueing under the
        same `idempotency_key` is harmless (arq dedupes on `_job_id`), but the age filter
        keeps the sweeper's log honest about what it actually rescued.
        """
        stmt = (
            select(KnowledgeJob)
            .where(
                KnowledgeJob.job_type == JobType.INGEST,
                KnowledgeJob.status == JobStatus.QUEUED,
                KnowledgeJob.error_code == ENQUEUE_FAILED,
                KnowledgeJob.created_at < older_than,
            )
            .order_by(KnowledgeJob.created_at)
            .limit(limit)
        )
        return (await self.session.scalars(stmt)).all()

    async def list_open_for_document(
        self, document_id: uuid.UUID, *, job_type: JobType
    ) -> Sequence[KnowledgeJob]:
        """Jobs of one kind for one document that have not reached a terminal status.

        Used by the deletion request to supersede an in-flight ingestion (R-39(8)) — the
        `FAILED`/`DOCUMENT_DELETED` half of §11's "delete while ingestion is running".
        `DEAD_LETTER` counts as terminal alongside `SUCCEEDED`/`FAILED`.
        """
        stmt = select(KnowledgeJob).where(
            KnowledgeJob.document_id == document_id,
            KnowledgeJob.job_type == job_type,
            KnowledgeJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
        return (await self.session.scalars(stmt)).all()

    async def has_job(self, document_id: uuid.UUID, *, job_type: JobType) -> bool:
        """Whether a job of this kind has ever been created for the document.

        The ingest swap guard's second signal (R-39(8)), and it needs to be a *second*
        signal rather than a nicety: the task walks `documents.status` through its stages
        from an ORM instance loaded before the embed window, so its own
        `set_status(INDEXING)` overwrites a `DELETE_PENDING` written concurrently and the
        status alone would read clean. A deletion job, once created, is never deleted — so
        "any DELETE job exists" is the one fact the ingest path cannot erase.
        """
        stmt = select(KnowledgeJob.id).where(
            KnowledgeJob.document_id == document_id,
            KnowledgeJob.job_type == job_type,
        )
        return (await self.session.scalars(stmt)).first() is not None

    async def count_for_document_version(
        self, document_id: uuid.UUID, *, job_type: JobType, document_version: int
    ) -> int:
        """How many jobs of this kind already target this version — the retry suffix `k`.

        Counting rather than parsing the previous `idempotency_key`: R-38(1) established
        that key format is a broker-dedupe detail, and nothing that has to be *unique*
        should be derived by string-surgery on it.
        """
        stmt = select(func.count()).where(
            KnowledgeJob.document_id == document_id,
            KnowledgeJob.job_type == job_type,
            KnowledgeJob.document_version == document_version,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def latest_ingest_version(self, document_id: uuid.UUID) -> int | None:
        """The highest `document_version` any ingestion job has targeted, or None.

        The retry target (R-39(5)). Deliberately **not** `documents.current_version`: under
        R-36(3) a replace leaves that pointer on the *old* version until its swap commits,
        so retrying a failed replace by reading it would rebuild the version that is
        currently serving. And deliberately not "the most recent job by `created_at`" —
        T-108's lesson is that timestamps tie; versions only ever increase, so `MAX` is the
        same answer without an ordering assumption.
        """
        stmt = select(func.max(KnowledgeJob.document_version)).where(
            KnowledgeJob.document_id == document_id,
            KnowledgeJob.job_type == JobType.INGEST,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_scoped(
        self, job_id: uuid.UUID, *, owner_id: uuid.UUID | None
    ) -> KnowledgeJob | None:
        """FR-ING-06 read: the job, but only if `owner_id` owns its document (R-39(6)).

        `owner_id=None` is the administrator path (FR-USR-04) and applies no scope. A job
        belonging to someone else returns `None` and the route renders `404`, so the
        response cannot confirm that the id exists (NFR-SEC-02).
        """
        stmt = (
            select(KnowledgeJob)
            .join(Document, Document.id == KnowledgeJob.document_id)
            .where(KnowledgeJob.id == job_id)
        )
        if owner_id is not None:
            stmt = stmt.where(Document.owner_id == owner_id)
        return (await self.session.scalars(stmt)).first()

    async def update_status(
        self,
        job: KnowledgeJob,
        status: JobStatus,
        *,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
    ) -> KnowledgeJob:
        """Move a job to `status`, stamping `started_at`/`completed_at` as it goes.

        `error_code=None` means "leave it alone", so a job that fails, retries and then
        succeeds would keep its stale diagnostics forever. `clear_error=True` is the
        explicit opt-in to wipe them — used on the SUCCEEDED transition and by the
        sweeper once it has re-dispatched.
        """
        job.status = status
        if progress is not None:
            job.progress = progress
        if clear_error:
            job.error_code = None
            job.error_message = None
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if status is JobStatus.RUNNING and job.started_at is None:
            job.started_at = datetime.now(UTC)
        if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD_LETTER):
            job.completed_at = datetime.now(UTC)
        await self.session.flush()
        return job

    async def requeue(self, job: KnowledgeJob) -> KnowledgeJob:
        """Return a finished-or-stalled job to `QUEUED` for another dispatch (R-39(2)).

        The deletion job's key is `delete:{document_id}` and `idempotency_key` is `UNIQUE`,
        so a repeated `DELETE` — the manual re-drive for a dead-lettered purge — cannot mint
        a second row and must reset this one. `completed_at` is cleared as well as the error:
        left behind, it would make FR-ING-06 report a running job as having finished at some
        point in the past. `attempt_count` is **not** reset — it is the cumulative record
        §11 asks for, and the worker's own dead-letter detection reads arq's `job_try`.
        """
        job.status = JobStatus.QUEUED
        job.progress = 0
        job.error_code = None
        job.error_message = None
        job.completed_at = None
        await self.session.flush()
        return job

    async def increment_attempt(self, job: KnowledgeJob) -> KnowledgeJob:
        job.attempt_count += 1
        await self.session.flush()
        return job
