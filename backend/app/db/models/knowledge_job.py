"""`knowledge_jobs` — async ingestion/deletion jobs (FR-ING-02/04/06).

Carries idempotency (`idempotency_key` unique, FR-ING-04), retry state
(`attempt_count`), a dead-letter terminal status, and diagnostics for `GET
/jobs/{id}` (FR-ING-06).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin
from app.db.enums import JobStatus, JobType, str_enum


class KnowledgeJob(CreatedAtMixin, Base):
    __tablename__ = "knowledge_jobs"
    __table_args__ = (Index("ix_knowledge_jobs_document_id", "document_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    job_type: Mapped[JobType] = mapped_column(str_enum(JobType), nullable=False)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
