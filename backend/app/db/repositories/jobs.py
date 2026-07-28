"""Knowledge-job repository (T-102). Idempotency + retry state (FR-ING-04/06)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.enums import JobStatus, JobType
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.base import BaseRepository

# Sentinel written by T-202 when the post-commit enqueue fails: the job row is committed
# and the API still returns 202, so this is real work nobody dispatched. The T-207 sweeper
# is what eventually dispatches it.
ENQUEUE_FAILED = "ENQUEUE_FAILED"


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

    async def increment_attempt(self, job: KnowledgeJob) -> KnowledgeJob:
        job.attempt_count += 1
        await self.session.flush()
        return job
