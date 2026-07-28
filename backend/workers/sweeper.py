"""Sweeper for ingestion jobs that were committed but never dispatched (T-207).

T-202's upload path enqueues **after** the commit — deliberately, because enqueueing inside
the transaction is the classic dual-write bug (the worker can pick up a row nobody else can
see yet). The cost of that ordering is a window: if the broker is down when the enqueue
runs, `_enqueue_quietly` records `error_code="ENQUEUE_FAILED"`, commits, and still returns
`202` to the user. That is the right answer for the request — the row is durable and
retryable per FR-ING-04 — but it leaves a document that will sit at `QUEUED` forever
because nothing ever dispatched it.

This cron closes that window. It is the *only* stuck-state sweeper in the design, and
deliberately so: a job that was dispatched and then interrupted by a worker crash needs no
help. arq's `finish_job` never ran, so the entry stays in the queue sorted-set and its
in-progress key eventually expires; a worker then re-picks it, and `persist_chunk_set`'s
unconditional `delete_by_version` makes the replay idempotent (§11, "worker crashes after
vector insertion"). A second sweeper aimed at those would race arq's own redelivery and
could double-dispatch.

**How long "eventually" is, measured rather than assumed (T-212).** arq guards against
double-processing with `arq:in-progress:{job_id}`, whose TTL is
``job_timeout + 10`` seconds (`arq/worker.py`), and it refuses to start any job whose key
still exists. So a SIGKILLed job is **not** re-picked promptly: it waits out the remainder
of that TTL — with `WORKER_JOB_TIMEOUT_SECONDS = 900` that is up to **~15 minutes**, during
which the document sits in whatever FR-ING-01 state it had reached and nothing signals that
recovery is pending. Nothing is lost (the queue entry survives, verified live), but the
latency is real and it is `WORKER_JOB_TIMEOUT_SECONDS` that sets it. That setting therefore
has two jobs pulling opposite ways — it must exceed the longest legitimate ingestion, and
it *is* the crash-recovery delay — which is why it stays a `# TBD(§8.4)` rather than a
number someone picked once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.config import get_settings
from app.db.repositories.jobs import KnowledgeJobRepository
from app.db.session import get_sessionmaker
from app.services.jobs import JobQueueError, get_job_queue

log = structlog.get_logger(__name__)


async def sweep_undispatched_jobs(ctx: dict[str, Any]) -> int:
    """Re-enqueue ingestion jobs stranded by a broker outage. Returns how many were sent."""
    settings = ctx.get("settings") or get_settings()
    sessionmaker = ctx.get("sessionmaker") or get_sessionmaker()
    queue = ctx.get("queue") or get_job_queue()

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker.sweep_min_age_seconds)

    async with sessionmaker() as session:
        repo = KnowledgeJobRepository(session)
        stranded = await repo.list_undispatched(
            older_than=cutoff, limit=settings.worker.sweep_batch_size
        )
        if not stranded:
            return 0

        dispatched = 0
        for job in stranded:
            try:
                await queue.enqueue_ingest(
                    job_id=job.id,
                    document_id=job.document_id,
                    idempotency_key=job.idempotency_key,
                )
            except JobQueueError as exc:
                # Still down. Leave the sentinel in place and try again next tick — the
                # rows are durable, so there is nothing to lose by waiting.
                log.warning("sweeper.enqueue_failed", job_id=str(job.id), error=str(exc))
                break

            # Clear the sentinel only after a successful dispatch, so a partial sweep
            # leaves the remainder visible to the next run. Re-enqueueing under the same
            # `idempotency_key` is safe regardless: arq dedupes on `_job_id`, and the task
            # itself short-circuits an already-succeeded job (FR-ING-04).
            await repo.update_status(job, job.status, clear_error=True)
            dispatched += 1

        await session.commit()

    if dispatched:
        log.info("sweeper.dispatched", jobs=dispatched, stranded=len(stranded))
    return dispatched
