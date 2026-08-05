"""Dependency readiness probes (T-106, NFR-REL-02).

Each probe touches one live dependency and returns a :class:`CheckResult` — never
raises. Probes are individually timed and bounded by ``_PROBE_TIMEOUT`` so a hung
dependency cannot stall the readiness endpoint; :func:`run_readiness_checks` fans
them out concurrently.

Coverage per NFR-REL-02:

* **database** — a ``SELECT 1`` on the async engine. The vector store is pgvector
  *inside the same Postgres* (NFR-REL-03 / §4.16), so this arm covers it too.
* **broker** — a Redis ``PING``. No shared Redis client exists (slowapi owns its
  storage string internally and exposes no handle; the arq worker is T-207), so the
  probe builds its own ephemeral ``redis.asyncio`` client from the broker URL.
* **object_storage** — :meth:`ObjectStorage.check_health` on the configured backend
  (T-201): ``HeadBucket`` for S3/MinIO, a writable-root check for the filesystem
  backend. This replaced the original ``httpx`` GET to ``/minio/health/live``, which
  R-29 adopted only to avoid front-running the T-201 client (spec Rev 0.6.8): the
  bucket check also proves the credentials and the bucket, and it is correct when the
  filesystem backend is selected, where MinIO may legitimately not be running at all.

**Worker readiness is a separate surface** (T-207, R-38(2)). NFR-REL-02 asks for readiness
"for the API and workers", and R-32 adds a ``clamd`` probe to the worker's — but folding
either into :func:`run_readiness_checks` would make the API report `503` and get pulled
from the load balancer because ClamAV is down, while it can still serve every chat and
retrieval request. So `/health/ready` keeps exactly the R-29 contract above, and
:func:`run_worker_readiness_checks` backs `/health/ready/worker`: two probe targets for two
deployables.

The worker arm adds:

* **clamav** — ``PING`` over the T-207 INSTREAM client. Only probed when
  ``SCANNER_BACKEND=clamav``; under ``structural`` there is no daemon to reach and
  reporting one down would be a false alarm.
* **worker** — the arq heartbeat key. arq's ``record_health`` rewrites it every
  ``health_check_interval`` with a TTL of ``interval + 1``, so the key's *existence* is
  the liveness signal and expiry does the timing for us — no clock comparison, and no
  false "alive" from a worker that died an hour ago.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

import redis.asyncio as aioredis
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine
from app.services.clamav import get_clamav_client
from app.services.jobs import WORKER_HEALTH_CHECK_KEY
from app.services.object_storage import get_object_storage

# Per-probe wall-clock ceiling. Provisional pending the §8.4 decision. Kept short so
# an unreachable dependency fails the probe fast rather than hanging the endpoint.
_PROBE_TIMEOUT = 2.0  # seconds — # TBD(§8.4)
_ERROR_MAX_LEN = 200


class CheckResult(BaseModel):
    """Outcome of a single dependency probe."""

    status: Literal["ok", "error"]
    latency_ms: float | None = None
    error: str | None = None


def _ok(started: float) -> CheckResult:
    return CheckResult(status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))


def _error(exc: BaseException) -> CheckResult:
    if isinstance(exc, TimeoutError):
        detail = f"probe timed out after {_PROBE_TIMEOUT}s"
    else:
        detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
    return CheckResult(status="error", error=detail[:_ERROR_MAX_LEN])


async def check_database() -> CheckResult:
    """Probe Postgres (and, transitively, the pgvector store) with ``SELECT 1``."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return _ok(started)
    except Exception as exc:  # noqa: BLE001 — a probe must classify, never propagate.
        return _error(exc)


async def check_broker() -> CheckResult:
    """Probe the Redis broker (arq) with ``PING`` via an ephemeral client."""
    started = time.perf_counter()
    client: aioredis.Redis | None = None
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            client = aioredis.from_url(
                get_settings().redis.url, socket_connect_timeout=_PROBE_TIMEOUT
            )
            await client.ping()
        return _ok(started)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    finally:
        if client is not None:
            await client.aclose()


async def check_object_storage() -> CheckResult:
    """Probe the configured object-storage backend (T-201)."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            await get_object_storage().check_health()
        return _ok(started)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def check_clamav() -> CheckResult:
    """Probe `clamd` with ``PING`` (R-32; worker readiness only)."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            await get_clamav_client().ping()
        return _ok(started)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def check_worker() -> CheckResult:
    """Probe for a live arq worker via its heartbeat key.

    arq rewrites the key every ``health_check_interval`` with a TTL of ``interval + 1``,
    so a missing key means no worker has checked in within that window. Reading existence
    rather than parsing the payload's timestamp keeps Redis's expiry as the single clock —
    there is nothing here to skew against the worker's.
    """
    started = time.perf_counter()
    client: aioredis.Redis | None = None
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            client = aioredis.from_url(
                get_settings().redis.url, socket_connect_timeout=_PROBE_TIMEOUT
            )
            raw = await client.get(WORKER_HEALTH_CHECK_KEY)
        if raw is None:
            interval = get_settings().worker.heartbeat_seconds
            return CheckResult(
                status="error",
                error=f"no arq worker heartbeat in the last {interval:g}s",
            )
        return _ok(started)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    finally:
        if client is not None:
            await client.aclose()


async def run_readiness_checks() -> tuple[bool, ReadinessResponse]:
    """Run every probe concurrently and aggregate.

    Returns ``(all_ok, payload)`` where ``payload`` is the JSON-serialisable body
    :class:`ReadinessResponse`. Overall
    status is ``ok`` only when every probe passed.
    """
    database, broker, object_storage = await asyncio.gather(
        check_database(), check_broker(), check_object_storage()
    )
    checks = {"database": database, "broker": broker, "object_storage": object_storage}
    return _aggregate(checks)


async def run_worker_readiness_checks() -> tuple[bool, ReadinessResponse]:
    """Readiness for the ingestion worker deployable (T-207, R-38(2)).

    The worker's own dependency set — everything the API needs, because the ingestion task
    reads the database, the broker and object storage, plus the scanner it must not run
    without. Same ``(all_ok, payload)`` shape and same 200/503 rule as the API arm, so an
    orchestrator configures it identically against a different path.
    """
    scanner_enabled = get_settings().scanner.backend == "clamav"

    database, broker, object_storage, worker, clamav = await asyncio.gather(
        check_database(),
        check_broker(),
        check_object_storage(),
        check_worker(),
        check_clamav() if scanner_enabled else _skipped("SCANNER_BACKEND=structural"),
    )
    checks = {
        "database": database,
        "broker": broker,
        "object_storage": object_storage,
        "worker": worker,
        "clamav": clamav,
    }
    return _aggregate(checks)


async def _skipped(reason: str) -> CheckResult:
    """A probe that does not apply to this configuration. Counts as passing."""
    return CheckResult(status="ok", error=f"not probed: {reason}")


class ReadinessResponse(BaseModel):
    """NFR-REL-02's readiness body, on both arms (T-405).

    Typed so the `503` branch is expressible in the schema at all: the status code is set
    imperatively on the response object, so FastAPI cannot infer it, and before T-405 the whole
    body was an untyped `dict[str, object]`.
    """

    status: Literal["ok", "degraded"]
    checks: dict[str, CheckResult]


def _aggregate(checks: dict[str, CheckResult]) -> tuple[bool, ReadinessResponse]:
    all_ok = all(check.status == "ok" for check in checks.values())
    return all_ok, ReadinessResponse(
        status="ok" if all_ok else "degraded",
        checks=checks,
    )
