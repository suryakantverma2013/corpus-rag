"""Background-job queue seam (T-202; the worker itself is T-207).

FR-ING-02 requires the upload endpoint to *enqueue* the ingestion task before returning
its `202`, but the arq worker is T-207 and does not exist yet. This module is the seam
that lets T-202 finish without front-running it — the same move T-106 made when it probed
MinIO over httpx rather than reach for the object-storage client T-201 had not written.

The contract is a pair of constants and one protocol:

* :data:`INGEST_TASK_NAME` / :data:`DELETE_TASK_NAME` — the arq function names the worker
  must register (T-207 and T-208 respectively).
* :class:`JobQueue` — ``enqueue_ingest`` / ``enqueue_delete`` plus ``aclose``.

arq resolves function names **worker-side**, so enqueueing a name nothing implements yet
succeeds and the job simply waits on the broker. Payloads are IDs only, never bytes: the
worker re-reads the row and streams the object itself.

Two backends, mirroring the R-19 pattern that object storage established: :class:`ArqJobQueue`
for real deployments and :class:`NullJobQueue` (``QUEUE_BACKEND=none``) for dev and CI so
the API runs without Redis.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Annotated, Protocol, runtime_checkable

import structlog
from fastapi import Depends

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

#: The arq function name T-207 registers in `workers/main.py`'s `WorkerSettings.functions`.
#: Changing it silently orphans every queued job — treat it as a wire contract.
INGEST_TASK_NAME = "ingest_document"

#: The FR-ING-05 deletion task (T-208), registered in the same place under the same rule.
DELETE_TASK_NAME = "delete_document"

#: The FR-EVL-01 post-hoc evaluation task (T-309, R-50), same rule again.
#:
#: Unlike the two above this job has no `knowledge_jobs` row behind it — there is nothing for
#: a user to retry and nothing to show in the FR-KBM-04 list, because a message whose scores
#: never arrive simply renders without chips (FR-EVL-01: a response *may* carry them). So the
#: payload is one id, and the broker's own `_job_id` is the whole idempotency story.
EVALUATE_TASK_NAME = "evaluate_message"

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


def evaluation_idempotency_key(message_id: uuid.UUID, content: str) -> str:
    """Broker-level dedup key for an FR-EVL-01 evaluation job (T-309, R-50).

    The **answer text is part of the key**, and that is the whole point. Keying on the message
    id alone would be correct for redelivery but wrong for FR-MSG-08 Regenerate, which
    replaces `messages.content` in place: the second evaluation would carry the same key, arq
    would recognise it as a duplicate, and the message would keep the scores of an answer that
    no longer exists — silently, and permanently. Hashing the content makes a regenerated
    answer a genuinely different job while a redelivery of the same one stays a duplicate.

    **Binds T-402/T-403:** Regenerate must also clear `messages.evaluation`, or the worker's
    already-evaluated skip will drop the re-run before it starts.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"eval:{message_id}:{digest}"


@runtime_checkable
class JobQueue(Protocol):
    """The seam the upload service depends on."""

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """Hand an ingestion job to the broker. Raises :class:`JobQueueError`."""
        ...

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """Hand an FR-ING-05 deletion job to the broker. Raises :class:`JobQueueError`."""
        ...

    async def enqueue_evaluate(self, *, message_id: uuid.UUID, idempotency_key: str) -> None:
        """Hand an FR-EVL-01 evaluation job to the broker. Raises :class:`JobQueueError`.

        **Binds T-402:** call this *after* the transaction that writes the AI `messages` row
        commits, never inside it — the T-202 dual-write lesson, where enqueueing inside the
        transaction lets the worker read a row nobody else can see yet. A failed enqueue must
        not fail the turn: the answer is already served and its scores are optional.

        See :func:`evaluation_idempotency_key` for why the key includes the answer text.
        """
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
        self._log(INGEST_TASK_NAME, job_id=job_id, document_id=document_id)

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self._log(DELETE_TASK_NAME, job_id=job_id, document_id=document_id)

    async def enqueue_evaluate(self, *, message_id: uuid.UUID, idempotency_key: str) -> None:
        log.info("job_queue.noop", task=EVALUATE_TASK_NAME, message_id=str(message_id))

    @staticmethod
    def _log(task: str, *, job_id: uuid.UUID, document_id: uuid.UUID) -> None:
        log.info("job_queue.noop", task=task, job_id=str(job_id), document_id=str(document_id))

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
        await self._enqueue(
            INGEST_TASK_NAME,
            job_id=job_id,
            document_id=document_id,
            idempotency_key=idempotency_key,
        )

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        await self._enqueue(
            DELETE_TASK_NAME,
            job_id=job_id,
            document_id=document_id,
            idempotency_key=idempotency_key,
        )

    async def enqueue_evaluate(self, *, message_id: uuid.UUID, idempotency_key: str) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                pool = await self._get_pool()
                await pool.enqueue_job(EVALUATE_TASK_NAME, str(message_id), _job_id=idempotency_key)
        except Exception as exc:
            raise JobQueueError(str(exc)) from exc

    async def _enqueue(
        self, task: str, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                pool = await self._get_pool()
                # `_job_id` makes the broker itself reject a duplicate enqueue, layering
                # on top of the FR-ING-04 `knowledge_jobs.idempotency_key`. A retry of
                # the same document version is therefore a no-op at both levels — and it
                # is what makes a repeated `DELETE` on an in-flight deletion safe (R-39(2)).
                await pool.enqueue_job(
                    task,
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


#: Route dependency for the upload (T-202), delete and retry (T-208) surfaces.
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
