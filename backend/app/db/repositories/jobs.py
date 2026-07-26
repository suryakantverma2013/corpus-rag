"""Knowledge-job repository (T-102). Idempotency + retry state (FR-ING-04/06)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db.enums import JobStatus
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.base import BaseRepository


class KnowledgeJobRepository(BaseRepository[KnowledgeJob]):
    model = KnowledgeJob

    async def get_by_idempotency_key(self, idempotency_key: str) -> KnowledgeJob | None:
        stmt = select(KnowledgeJob).where(KnowledgeJob.idempotency_key == idempotency_key)
        return (await self.session.scalars(stmt)).first()

    async def update_status(
        self,
        job: KnowledgeJob,
        status: JobStatus,
        *,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> KnowledgeJob:
        job.status = status
        if progress is not None:
            job.progress = progress
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
