"""Worker wiring and the undispatched-job sweeper (T-207).

Two concerns that have nothing to do with ingestion itself but would each be silent in
production if wrong: the arq registration (a name mismatch orphans every queued job with
no error anywhere) and the sweeper that rescues jobs T-202 committed but could not enqueue.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, WorkerSettings, get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.jobs import ENQUEUE_FAILED, KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.services.jobs import (
    DELETE_TASK_NAME,
    EVALUATE_TASK_NAME,
    INGEST_TASK_NAME,
    WORKER_HEALTH_CHECK_KEY,
    JobQueueError,
)
from workers.main import WorkerSettings as ArqWorkerSettings
from workers.main import _sweep_minutes
from workers.sweeper import sweep_undispatched_jobs

# --- arq wiring ---------------------------------------------------------------------


def test_the_registered_task_names_match_the_enqueue_contract() -> None:
    """The API enqueues under these constants; a rename here orphans every queued job.

    Nothing errors when they disagree — arq simply never matches the queued jobs to a
    function, and documents sit at QUEUED (or DELETE_PENDING) forever. Hence a test rather
    than a comment.
    """
    names = [function.name for function in ArqWorkerSettings.functions]
    assert names == [INGEST_TASK_NAME, DELETE_TASK_NAME, EVALUATE_TASK_NAME]


def test_the_heartbeat_is_not_arqs_hour_long_default() -> None:
    """The health-check key's TTL *is* the `/health/ready/worker` signal (R-38(2)).

    At arq's 3600s default the probe would report a worker that died 59 minutes ago as
    healthy, which is worse than having no probe.
    """
    assert ArqWorkerSettings.health_check_interval <= 60
    assert ArqWorkerSettings.health_check_key == WORKER_HEALTH_CHECK_KEY


def test_the_health_check_key_matches_arqs_own_default() -> None:
    """`WORKER_HEALTH_CHECK_KEY` is spelled out so the API need not import arq."""
    constants = pytest.importorskip("arq.constants")
    expected = constants.default_queue_name + constants.health_check_key_suffix
    assert WORKER_HEALTH_CHECK_KEY == expected


def test_the_sweeper_is_scheduled() -> None:
    # arq prefixes cron function names with `cron:` to keep them out of the task namespace.
    assert [job.name for job in ArqWorkerSettings.cron_jobs] == ["cron:sweep_undispatched_jobs"]
    assert ArqWorkerSettings.cron_jobs[0].run_at_startup is True


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300, {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        (60, set(range(60))),
        (10, set(range(60))),  # sub-minute intervals clamp to every minute
    ],
)
def test_the_sweep_interval_becomes_a_minute_set(seconds: float, expected: set[int]) -> None:
    assert _sweep_minutes(seconds) == expected


def test_worker_settings_reject_incoherent_values() -> None:
    with pytest.raises(ValueError, match="WORKER_MAX_TRIES"):
        WorkerSettings(max_tries=0)
    with pytest.raises(ValueError, match="WORKER_RETRY_MAX_SECONDS"):
        WorkerSettings(retry_base_seconds=60, retry_max_seconds=10)


# --- the sweeper --------------------------------------------------------------------


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(get_settings().database.url, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, DBAPIError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable for sweeper tests: {exc}")

    txn = await conn.begin()
    maker = async_sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield maker
    finally:
        if txn.is_active:
            await txn.rollback()
        await conn.close()
        await engine.dispose()


class _RecordingQueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        if self.fail:
            raise JobQueueError("broker down")
        self.calls.append(idempotency_key)

    async def aclose(self) -> None:
        return None


async def _job(
    sessions: async_sessionmaker[AsyncSession],
    *,
    error_code: str | None,
    age_seconds: float,
) -> uuid.UUID:
    async with sessions() as session:
        user = await UserRepository(session).upsert_from_claims(
            sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
        )
        kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
        document = Document(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="handbook.pdf",
            storage_uri="file:///handbook.pdf",
            checksum_sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.QUEUED,
        )
        session.add(document)
        await session.flush()
        job = KnowledgeJob(
            document_id=document.id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            document_version=1,
            error_code=error_code,
            idempotency_key=f"ingest:{document.id}:v1",
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        session.add(job)
        await session.commit()
        return job.id


def _ctx(sessions, queue: _RecordingQueue) -> dict[str, object]:  # noqa: ANN001
    return {
        "settings": Settings(worker=WorkerSettings(sweep_min_age_seconds=120)),
        "sessionmaker": sessions,
        "queue": queue,
    }


async def test_a_stranded_job_is_redispatched_and_its_sentinel_cleared(sessions) -> None:  # noqa: ANN001
    """T-202 returns 202 with `ENQUEUE_FAILED` when the broker is down — real work."""
    job_id = await _job(sessions, error_code=ENQUEUE_FAILED, age_seconds=600)
    queue = _RecordingQueue()

    assert await sweep_undispatched_jobs(_ctx(sessions, queue)) == 1

    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
    assert queue.calls == [job.idempotency_key]
    assert job.error_code is None
    assert job.status is JobStatus.QUEUED  # the worker, not the sweeper, moves it on


async def test_a_freshly_stranded_job_is_left_to_the_request_that_made_it(sessions) -> None:  # noqa: ANN001
    """The age filter keeps the sweeper from racing an enqueue that is still in flight."""
    await _job(sessions, error_code=ENQUEUE_FAILED, age_seconds=5)
    queue = _RecordingQueue()

    assert await sweep_undispatched_jobs(_ctx(sessions, queue)) == 0
    assert queue.calls == []


async def test_an_ordinary_queued_job_is_not_swept(sessions) -> None:  # noqa: ANN001
    """Only the `ENQUEUE_FAILED` sentinel marks a job nobody dispatched.

    Everything else queued is already on the broker; re-enqueueing it would be noise at
    best, and the arq redelivery path already covers a crashed worker.
    """
    await _job(sessions, error_code=None, age_seconds=600)
    queue = _RecordingQueue()

    assert await sweep_undispatched_jobs(_ctx(sessions, queue)) == 0


async def test_a_still_down_broker_leaves_the_sentinel_in_place(sessions) -> None:  # noqa: ANN001
    job_id = await _job(sessions, error_code=ENQUEUE_FAILED, age_seconds=600)

    assert await sweep_undispatched_jobs(_ctx(sessions, _RecordingQueue(fail=True))) == 0

    async with sessions() as session:
        job = await session.get(KnowledgeJob, job_id)
        assert job is not None
    assert job.error_code == ENQUEUE_FAILED  # still visible to the next tick


async def test_the_repository_only_returns_undispatched_ingest_jobs(sessions) -> None:  # noqa: ANN001
    stranded = await _job(sessions, error_code=ENQUEUE_FAILED, age_seconds=600)
    await _job(sessions, error_code=None, age_seconds=600)
    await _job(sessions, error_code="PARSE_FAILED", age_seconds=600)

    async with sessions() as session:
        found = await KnowledgeJobRepository(session).list_undispatched(
            older_than=datetime.now(UTC)
        )
    assert [job.id for job in found] == [stranded]
