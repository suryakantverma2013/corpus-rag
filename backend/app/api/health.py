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

Both readiness routes set their status code **imperatively** on the response, because the same
body is served either way and only the code differs. FastAPI cannot infer a status it never
sees, so the ``503`` is declared explicitly (T-405) — otherwise the one outcome an orchestrator
acts on would be absent from the schema.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.services.health import ReadinessResponse, run_readiness_checks, run_worker_readiness_checks

router = APIRouter(tags=["system"])

#: Same body, different code. Declared on both readiness routes.
_DEGRADED = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ReadinessResponse,
        "description": "At least one dependency failed its probe (NFR-REL-02).",
    }
}


class LivenessResponse(BaseModel):
    """Liveness is a constant: reaching the handler at all is the answer."""

    status: Literal["ok"]


@router.get("/health", summary="Liveness probe")
async def liveness() -> LivenessResponse:
    """Liveness probe — the process is up (no dependency checks)."""
    return LivenessResponse(status="ok")


@router.get("/health/ready", responses=_DEGRADED, summary="API readiness probe")
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness probe — every dependency the *API* serves requests from (NFR-REL-02)."""
    all_ok, payload = await run_readiness_checks()
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload


@router.get("/health/ready/worker", responses=_DEGRADED, summary="Worker readiness probe")
async def worker_readiness(response: Response) -> ReadinessResponse:
    """Readiness probe for the ingestion worker deployable (T-207, R-38(2))."""
    all_ok, payload = await run_worker_readiness_checks()
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
