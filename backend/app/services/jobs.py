"""Background-job queue seam (T-202; the worker itself is T-207).

FR-ING-02 requires the upload endpoint to *enqueue* the ingestion task before returning
its `202`, but the arq worker is T-207 and does not exist yet. This module is the seam
that lets T-202 finish without front-running it — the same move T-106 made when it probed
MinIO over httpx rather than reach for the object-storage client T-201 had not written.

The contract is one constant and one protocol:

* :data:`INGEST_TASK_NAME` — the arq function name T-207 must register.
* :class:`JobQueue` — ``enqueue_ingest`` plus ``aclose``.

arq resolves function names **worker-side**, so enqueueing a name nothing implements yet
succeeds and the job simply waits on the broker. Payloads are IDs only, never bytes: the
worker re-reads the row and streams the object itself.

Two backends, mirroring the R-19 pattern that object storage established: :class:`ArqJobQueue`
for real deployments and :class:`NullJobQueue` (``QUEUE_BACKEND=none``) for dev and CI so
the API runs without Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Protocol, runtime_checkable

import structlog
from fastapi import Depends

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

#: The arq function name T-207 registers in `workers/main.py`'s `WorkerSettings.functions`.
#: Changing it silently orphans every queued job — treat it as a wire contract.
INGEST_TASK_NAME = "ingest_document"

#: Redis key the worker heartbeats into, and the `/health/ready/worker` liveness signal
#: (R-38(2)). arq writes it with a TTL of `health_check_interval + 1`, so its mere
#: existence means "a worker was alive within the last interval".
#:
#: Spelled out rather than derived from `arq.constants` so the API process — which must
#: read this key but has no reason to import arq — and `workers.main`, which sets it
#: explicitly, cannot drift apart. The value matches arq's own default
#: (`default_queue_name + health_check_key_suffix`), and a test pins that equivalence.
WORKER_HEALTH_CHECK_KEY = "arq:queue:health-check"


class JobQueueError(Exception):
    """The job could not be handed to the broker."""


@runtime_checkable
class JobQueue(Protocol):
    """The seam the upload service depends on."""

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """Hand an ingestion job to the broker. Raises :class:`JobQueueError`."""
        ...

    async def aclose(self) -> None:
        """Release any pooled connections."""
        ...


class NullJobQueue:
    """No-op queue — logs and returns (``QUEUE_BACKEND=none``).

    For local work without Redis and for the test suite. The `knowledge_jobs` row is
    still written, so a job enqueued here is recoverable once a real worker runs.
    """

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        log.info(
            "job_queue.noop",
            task=INGEST_TASK_NAME,
            job_id=str(job_id),
            document_id=str(document_id),
        )

    async def aclose(self) -> None:
        return None


class ArqJobQueue:
    """arq/Redis queue (R-18).

    The pool is built lazily under a lock on first enqueue — a cold Redis must not stop
    the API from booting, exactly as with the S3 client in `object_storage`; readiness
    (NFR-REL-02) is what reports the outage. It is bound to the event loop that built it,
    so the app lifespan closes it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._redis_url = settings.redis.url
        self._timeout = settings.queue.enqueue_timeout_seconds
        self._pool = None
        self._lock = asyncio.Lock()

    async def _get_pool(self):  # noqa: ANN202 — arq's ArqRedis is imported lazily
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                from arq.connections import RedisSettings, create_pool

                self._pool = await create_pool(RedisSettings.from_dsn(self._redis_url))
        return self._pool

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                pool = await self._get_pool()
                # `_job_id` makes the broker itself reject a duplicate enqueue, layering
                # on top of the FR-ING-04 `knowledge_jobs.idempotency_key`. A retry of
                # the same document version is therefore a no-op at both levels.
                await pool.enqueue_job(
                    INGEST_TASK_NAME,
                    str(document_id),
                    str(job_id),
                    _job_id=idempotency_key,
                )
        except Exception as exc:
            # Deliberately broad: redis/arq raise a wide, version-dependent error surface
            # (connection, timeout, protocol, serialization) and the caller's contract is
            # "a failed enqueue must not fail the upload" — the row is committed and
            # retryable. Narrowing this would let an unexpected client error escape and
            # turn a 202 into a 500.
            raise JobQueueError(str(exc)) from exc

    async def aclose(self) -> None:
        if self._pool is not None:
            # `ArqRedis` subclasses redis.asyncio.Redis, whose `close()` is a *coroutine*
            # (deprecated since 5.0.1) and which has no `wait_closed()` — calling those
            # leaves the connection pool open and emits a never-awaited warning. Verified
            # against the live broker.
            await self._pool.aclose()
        self._pool = None


# --- factory / DI ------------------------------------------------------------

_queue: JobQueue | None = None


def build_job_queue(settings: Settings | None = None) -> JobQueue:
    """Construct the backend named by ``QUEUE_BACKEND`` (no caching)."""
    settings = settings or get_settings()
    if settings.queue.backend == "none":
        return NullJobQueue()
    return ArqJobQueue(settings)


def get_job_queue() -> JobQueue:
    """Process-wide job queue — also usable as a FastAPI dependency.

    Cached because the arq pool holds connections bound to the running loop;
    :func:`close_job_queue` (app lifespan / test teardown) tears it down.
    """
    global _queue
    if _queue is None:
        _queue = build_job_queue()
    return _queue


async def close_job_queue() -> None:
    """Close and forget the cached instance."""
    global _queue
    if _queue is not None:
        await _queue.aclose()
        _queue = None


#: Route dependency for the upload surface (T-202) and, later, retry/replace (T-208/209).
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
