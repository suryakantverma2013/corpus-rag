"""Job-queue seam tests (T-202; the worker that consumes these is T-207).

The offline tests drive `ArqJobQueue` against an injected fake pool — the T-201 pattern
for exercising client logic without the service. The live test talks to a real broker and
skips when one is not running, which is how the `aclose()` bug (redis-py's `close()` is a
coroutine, and `wait_closed()` does not exist) was caught in the first place.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings, get_settings
from app.services.jobs import (
    INGEST_TASK_NAME,
    ArqJobQueue,
    JobQueue,
    JobQueueError,
    NullJobQueue,
    build_job_queue,
    close_job_queue,
    get_job_queue,
)


class _FakePool:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.fail = fail
        self.closed = False

    async def enqueue_job(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.calls.append((args, kwargs))

    async def aclose(self) -> None:
        self.closed = True


def _queue_with(pool: _FakePool) -> ArqJobQueue:
    queue = ArqJobQueue(get_settings())
    queue._pool = pool  # noqa: SLF001 — injecting the client is the point of the test
    return queue


# ---- protocol conformance ----


def test_both_backends_satisfy_the_protocol() -> None:
    assert isinstance(NullJobQueue(), JobQueue)
    assert isinstance(ArqJobQueue(get_settings()), JobQueue)


# ---- Null backend ----


async def test_null_queue_is_a_noop() -> None:
    await NullJobQueue().enqueue_ingest(
        job_id=uuid.uuid4(), document_id=uuid.uuid4(), idempotency_key="k"
    )
    await NullJobQueue().aclose()


# ---- arq backend (offline) ----


async def test_enqueue_uses_task_name_and_idempotency_key() -> None:
    pool = _FakePool()
    queue = _queue_with(pool)
    job_id, document_id = uuid.uuid4(), uuid.uuid4()

    await queue.enqueue_ingest(
        job_id=job_id, document_id=document_id, idempotency_key="ingest:x:v1"
    )

    (args, kwargs) = pool.calls[0]
    assert args[0] == INGEST_TASK_NAME
    # IDs only — never payload bytes; the worker re-reads the row and streams the object.
    assert args[1:] == (str(document_id), str(job_id))
    # `_job_id` gives broker-level dedup on top of the FR-ING-04 row key.
    assert kwargs["_job_id"] == "ingest:x:v1"


async def test_broker_errors_are_translated() -> None:
    queue = _queue_with(_FakePool(fail=True))
    with pytest.raises(JobQueueError):
        await queue.enqueue_ingest(
            job_id=uuid.uuid4(), document_id=uuid.uuid4(), idempotency_key="k"
        )


async def test_aclose_releases_the_pool() -> None:
    pool = _FakePool()
    queue = _queue_with(pool)
    await queue.aclose()
    assert pool.closed is True
    assert queue._pool is None  # noqa: SLF001


# ---- factory / DI ----


def test_backend_selected_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    monkeypatch.setattr(settings.queue, "backend", "none")
    assert isinstance(build_job_queue(settings), NullJobQueue)
    monkeypatch.setattr(settings.queue, "backend", "arq")
    assert isinstance(build_job_queue(settings), ArqJobQueue)


async def test_get_job_queue_is_cached_and_closeable() -> None:
    try:
        assert get_job_queue() is get_job_queue()
    finally:
        await close_job_queue()


# ---- live broker ----


async def test_live_enqueue_lands_on_the_broker() -> None:
    """Round-trip against a real Redis; skips when the dev broker is not running.

    Asserts via `Job.info()` rather than `pool.queued_jobs()`: the latter raises if *any*
    id in the queue lacks a job hash, so one leftover from an unrelated run would fail
    this test for the wrong reason. Teardown removes both the hash and the queue entry —
    dropping only the hash is exactly what leaves that poison behind.
    """
    connections = pytest.importorskip("arq.connections")
    constants = pytest.importorskip("arq.constants")
    jobs_mod = pytest.importorskip("arq.jobs")

    settings = get_settings()
    try:
        pool = await connections.create_pool(connections.RedisSettings.from_dsn(settings.redis.url))
        await pool.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis not reachable: {exc}")

    queue = ArqJobQueue(settings)
    key = f"test:{uuid.uuid4()}"
    try:
        await queue.enqueue_ingest(
            job_id=uuid.uuid4(), document_id=uuid.uuid4(), idempotency_key=key
        )
        info = await jobs_mod.Job(key, pool).info()
        assert info is not None
        assert info.function == INGEST_TASK_NAME
    finally:
        await pool.delete(f"{constants.job_key_prefix}{key}")
        await pool.zrem(constants.default_queue_name, key)
        await queue.aclose()
        await pool.aclose()
