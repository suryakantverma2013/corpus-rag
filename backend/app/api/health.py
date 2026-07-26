"""System probes: liveness + dependency readiness (T-106, NFR-REL-02).

Mounted at the app root (unversioned) — orchestrators expect stable, unversioned
probe paths, so these sit outside the ``/api/v1`` surface (ruling R-29). Both are
unauthenticated and unthrottled: a probe must be reachable when auth or Redis is the
very thing that is down.

* ``GET /health`` — liveness: the process is up. No dependency checks.
* ``GET /health/ready`` — readiness: probes DB, broker, object storage. ``200`` when
  all pass, ``503`` when any fails; the body always reports per-check detail.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.services.health import run_readiness_checks

router = APIRouter(tags=["system"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Liveness probe — the process is up (no dependency checks)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Readiness probe — every live dependency must answer (NFR-REL-02)."""
    all_ok, payload = await run_readiness_checks()
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
