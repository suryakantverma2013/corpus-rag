"""Readiness endpoint + dependency probes (T-106, NFR-REL-02).

The endpoint tests monkeypatch the ``check_*`` probes so they run without live
Postgres/Redis/MinIO and assert the 200/503 aggregation contract. One guarded test
exercises the real DB probe against the local ``corpus`` database, skipping (like the
conftest ``session`` fixture) when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.services import health
from app.services.health import CheckResult


@pytest.fixture
async def probe_client() -> AsyncIterator[httpx.AsyncClient]:
    """Self-contained client — probes are monkeypatched, so no DB/auth is needed."""
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _patch_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok() -> CheckResult:
        return CheckResult(status="ok", latency_ms=1.0)

    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_broker", _ok)
    monkeypatch.setattr(health, "check_object_storage", _ok)


async def test_liveness_ok(probe_client: httpx.AsyncClient) -> None:
    resp = await probe_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_all_up(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)

    resp = await probe_client.get("/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "broker", "object_storage"}
    assert all(check["status"] == "ok" for check in body["checks"].values())


async def test_readiness_broker_down_returns_503(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)

    async def _broker_down() -> CheckResult:
        return CheckResult(status="error", error="ConnectionError: refused")

    monkeypatch.setattr(health, "check_broker", _broker_down)

    resp = await probe_client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["broker"]["status"] == "error"
    assert body["checks"]["broker"]["error"] == "ConnectionError: refused"
    # Healthy dependencies are still reported as ok alongside the failed one.
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["object_storage"]["status"] == "ok"


async def test_database_probe_against_local_db() -> None:
    """The real DB probe succeeds when the local ``corpus`` database is up (else skip)."""
    result = await health.check_database()
    if result.status == "error":
        pytest.skip(f"Postgres not reachable for readiness probe: {result.error}")
    assert result.status == "ok"
    assert result.latency_ms is not None
