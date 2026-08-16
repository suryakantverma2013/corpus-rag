"""arq worker entrypoint (T-207).

Run it with::

    uv run arq workers.main.WorkerSettings

**The registered function name is a wire contract.** T-202 already enqueues under
``app.services.jobs.INGEST_TASK_NAME``, so the name arq registers must equal that constant
exactly — a rename here silently orphans every job already sitting on the broker, with no
error anywhere. Hence the explicit ``func(..., name=INGEST_TASK_NAME)`` below and the test
that asserts the two agree.

**Startup builds the loop-bound clients.** The S3 client pools connections bound to the
event loop that created them, and the OpenAI client is the same; both are process-wide
singletons reached through `get_*` accessors, and arq runs its own loop. Building them in
``on_startup`` puts them on the right loop and gives ``on_shutdown`` something to close, so
a worker restart does not leak sockets. The database engine is `lru_cache`d for the same
reason and is disposed alongside them.

Note there is deliberately **no in-process/`BackgroundTasks` fallback** (R-38(3)): R-31
requires parsing and malware screening to happen in the worker, isolated from the API
process, so `QUEUE_BACKEND` offers only `arq` and `none`. Dev runs this worker for real,
with `EMBEDDING_BACKEND=fake` and `SCANNER_BACKEND=structural` if the machine cannot host
OpenAI credentials or a 2 GB `clamd`.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings as ArqRedisSettings
from arq.worker import func

from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.ingestion.scanner import build_scanner
from app.logging_config import configure_logging
from app.services.clamav import close_clamav_client, get_clamav_client
from app.services.embeddings import close_embedding_client, get_embedding_client
from app.services.jobs import (
    DELETE_TASK_NAME,
    EVALUATE_TASK_NAME,
    INGEST_TASK_NAME,
    WORKER_HEALTH_CHECK_KEY,
    close_job_queue,
    get_job_queue,
)
from app.services.llm import close_chat_client, get_chat_client
from app.services.object_storage import close_object_storage, get_object_storage
from workers.delete import delete_document
from workers.evaluate import evaluate_message
from workers.ingest import ingest_document
from workers.retention import prune_checkpoint_history, prune_turn_telemetry
from workers.sweeper import sweep_undispatched_jobs

log = structlog.get_logger(__name__)

_settings = get_settings()


async def on_startup(ctx: dict[str, Any]) -> None:
    """Build every dependency once, on arq's loop, and hand them to the tasks via `ctx`."""
    # Before the first log call. The worker previously inherited structlog's library
    # defaults, which means its output format was decided by whatever happened to be
    # installed — and a transitive `rich` turns exception logs into box-drawing characters
    # that raise `UnicodeEncodeError` on a non-UTF-8 stream, *inside* the logging call. See
    # `app.logging_config`.
    configure_logging()

    settings = get_settings()
    ctx["settings"] = settings
    ctx["sessionmaker"] = get_sessionmaker()
    ctx["storage"] = get_object_storage()
    ctx["embedder"] = get_embedding_client()
    ctx["scanner"] = build_scanner(settings)
    ctx["queue"] = get_job_queue()
    ctx["chat"] = get_chat_client()

    if settings.scanner.backend == "clamav":
        # Surface a dead `clamd` at startup rather than one document at a time. This does
        # not gate the worker: R-32 makes the scanner fail *closed* per job, so starting
        # with clamd down is safe (every job retries) and refusing to start would mean a
        # slow clamd boot took the whole worker with it.
        client = get_clamav_client()
        try:
            await client.ping()
            log.info("worker.clamav_ready", endpoint=client.endpoint)
        except Exception as exc:  # noqa: BLE001 — advisory only
            log.warning("worker.clamav_unreachable", endpoint=client.endpoint, error=str(exc))

    log.info(
        "worker.started",
        scanner=settings.scanner.backend,
        embeddings=settings.embedding.backend,
        storage=settings.storage.backend,
        max_tries=settings.worker.max_tries,
    )


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Close everything `on_startup` opened, so a restart does not leak connections."""
    await close_object_storage()
    await close_embedding_client()
    await close_clamav_client()
    await close_job_queue()
    await close_chat_client()
    await get_engine().dispose()
    log.info("worker.stopped")


def _sweep_minutes(interval_seconds: float) -> set[int]:
    """Turn a seconds interval into arq's minute set.

    arq schedules on wall-clock fields, not intervals, so the setting is expressed the way
    an operator thinks about it and converted here. A step that does not divide 60 evenly
    gives a slightly irregular gap at the top of each hour — immaterial for a sweeper whose
    whole job is to be eventually consistent.
    """
    step = max(1, min(30, round(interval_seconds / 60)))
    return set(range(0, 60, step))


class WorkerSettings:
    """arq worker configuration. Referenced by path on the command line, not instantiated."""

    functions = [
        # The names are the contract with the API's enqueue — never let them drift.
        func(ingest_document, name=INGEST_TASK_NAME, timeout=_settings.worker.job_timeout_seconds),
        # Deletion shares the timeout, though it needs far less of it: it holds no parse and
        # no embedding call, only a prefix delete and two short transactions.
        func(delete_document, name=DELETE_TASK_NAME, timeout=_settings.worker.job_timeout_seconds),
        # Evaluation gets the same ceiling, and needs a real share of it: five judge calls per
        # message on the `LLM_EVAL_*` budget. It is the one task here whose timeout expiring
        # costs nothing a user sees — the answer was served long before this ran (R-50).
        func(
            evaluate_message,
            name=EVALUATE_TASK_NAME,
            timeout=_settings.worker.job_timeout_seconds,
        ),
    ]

    cron_jobs = [
        cron(
            sweep_undispatched_jobs,
            minute=_sweep_minutes(_settings.worker.sweep_interval_seconds),
            second=0,
            # A worker coming up after a broker outage should rescue stranded jobs
            # immediately rather than waiting for the next slot.
            run_at_startup=True,
            # `unique` keeps a multi-worker deployment from sweeping the same rows N times.
            unique=True,
        ),
        # Checkpoint retention (OI-30, R-65). LangGraph keeps every superstep's checkpoint
        # and only the latest is ever read, so the store grows without bound.
        # `run_at_startup` is deliberately **False**, unlike the sweeper above: that one
        # rescues stranded work and is urgent after an outage, while this only reclaims
        # disk — and a worker restart loop would otherwise re-run a table-wide delete every
        # boot. `_settings.checkpointer.retention_interval_seconds <= 0` disables it, so
        # the job is only registered when it is wanted.
        *(
            [
                cron(
                    prune_checkpoint_history,
                    minute=_sweep_minutes(_settings.checkpointer.retention_interval_seconds),
                    second=30,  # off the sweeper's :00, so the two never contend
                    run_at_startup=False,
                    unique=True,
                )
            ]
            if _settings.checkpointer.retention_interval_seconds > 0
            else []
        ),
        # Telemetry retention (NFR-OBS-04, R-79(3)). Registered only when there is a horizon
        # *and* a cadence: `TELEMETRY_RETENTION_DAYS = 0` means keep forever, so at 0 the job
        # must not exist rather than run and find nothing — a scheduled destructive sweep
        # whose guard lives only inside the task is one refactor away from being a bug.
        # Second :15, off both the :00 sweeper and the :30 checkpoint prune.
        *(
            [
                cron(
                    prune_turn_telemetry,
                    minute=_sweep_minutes(_settings.telemetry.retention_interval_seconds),
                    second=15,
                    run_at_startup=False,
                    unique=True,
                )
            ]
            if (
                _settings.telemetry.retention_days > 0
                and _settings.telemetry.retention_interval_seconds > 0
            )
            else []
        ),
    ]

    redis_settings = ArqRedisSettings.from_dsn(_settings.redis.url)

    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)

    max_jobs = _settings.worker.max_jobs
    job_timeout = _settings.worker.job_timeout_seconds
    max_tries = _settings.worker.max_tries

    # arq's default is 3600s. `record_health` writes the health-check key with a TTL of
    # `interval + 1`, and that TTL *is* the `/health/ready/worker` liveness signal
    # (R-38(2)) — at the default the probe would report a worker that died 59 minutes ago
    # as healthy.
    health_check_interval = _settings.worker.heartbeat_seconds
    # Set explicitly rather than letting arq derive it, so the API's probe and this worker
    # read and write the same key by construction.
    health_check_key = WORKER_HEALTH_CHECK_KEY

    # Results are read by nobody: FR-ING-06 reports progress from `knowledge_jobs`, not
    # from arq's result store, because the job row outlives any Redis retention.
    keep_result = 0
