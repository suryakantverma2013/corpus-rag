"""System probes: liveness + dependency readiness (T-106, NFR-REL-02).

Mounted at the app root (unversioned) — orchestrators expect stable, unversioned
probe paths, so these sit outside the ``/api/v1`` surface (ruling R-29). Both are
unauthenticated and unthrottled: a probe must be reachable when auth or Redis is the
very thing that is down.

* ``GET /health`` — liveness: the process is up. No dependency checks.
* ``GET /health/ready`` — API readiness: probes DB, broker, object storage. ``200`` when
  all pass, ``503`` when any fails; the body always reports per-check detail.
* ``GET /health/ready/worker`` — ingestion-worker readiness (T-207): the above plus
  ``clamd`` and the arq heartbeat.

**Two readiness paths, not one** (R-38(2)). NFR-REL-02 asks for readiness for the API *and*
workers, and R-32 requires a ``clamd`` probe on the worker's. Merging them would take the
API out of the load balancer whenever ClamAV or the worker is down — while it can still
serve every chat and retrieval request. Both endpoints live on the API process because the
worker runs no HTTP server; the worker arm answers "is a worker alive and are its
dependencies up", which is what an orchestrator actually needs to know.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.services.health import run_readiness_checks, run_worker_readiness_checks

router = APIRouter(tags=["system"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Liveness probe — the process is up (no dependency checks)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Readiness probe — every dependency the *API* serves requests from (NFR-REL-02)."""
    all_ok, payload = await run_readiness_checks()
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@router.get("/health/ready/worker")
async def worker_readiness(response: Response) -> dict[str, object]:
    """Readiness probe for the ingestion worker deployable (T-207, R-38(2))."""
    all_ok, payload = await run_worker_readiness_checks()
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
