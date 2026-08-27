"""Readiness endpoint + dependency probes (T-106, NFR-REL-02).

The endpoint tests monkeypatch the ``check_*`` probes so they run without live
Postgres/Redis/MinIO and assert the 200/503 aggregation contract. One guarded test
exercises the real DB probe against the local ``corpus`` database, skipping (like the
conftest ``session`` fixture) when Postgres is unreachable.

Extended by T-207 with ``/health/ready/worker`` (R-38(2)). The test that matters most
there is ``test_clamav_down_takes_the_worker_probe_down_but_not_the_api`` — the whole
reason for splitting the two paths is that ClamAV being unavailable must not evict the API
from a load balancer while it can still serve every chat and retrieval request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from app.config import ParserSettings, ScannerSettings, Settings
from app.main import create_app
from app.services import health
from app.services.health import CheckResult
from app.services.jobs import WORKER_HEALTH_CHECK_KEY


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
    monkeypatch.setattr(health, "check_worker", _ok)
    monkeypatch.setattr(health, "check_clamav", _ok)
    monkeypatch.setattr(health, "check_ocr", _ok)


def _down(error: str):  # noqa: ANN202
    async def _probe() -> CheckResult:
        return CheckResult(status="error", error=error)

    return _probe


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


# --- worker readiness (T-207, R-38(2)) --------------------------------------------


async def test_worker_readiness_all_up(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)

    resp = await probe_client.get("/health/ready/worker")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {
        "database",
        "broker",
        "object_storage",
        "worker",
        "clamav",
        "ocr",
    }


async def test_a_missing_heartbeat_fails_the_worker_probe(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(health, "check_worker", _down("no arq worker heartbeat"))

    resp = await probe_client.get("/health/ready/worker")

    assert resp.status_code == 503
    assert resp.json()["checks"]["worker"]["status"] == "error"


async def test_clamav_down_takes_the_worker_probe_down_but_not_the_api(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-38(2)'s entire justification, asserted directly.

    One unreachable ClamAV, two different answers: the worker cannot ingest (R-32 fails
    closed), but the API can still serve every chat and retrieval request, so evicting it
    from the load balancer would be an outage manufactured out of a degraded dependency.
    """
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(health, "check_clamav", _down("ConnectionRefusedError"))

    worker = await probe_client.get("/health/ready/worker")
    api = await probe_client.get("/health/ready")

    assert worker.status_code == 503
    assert worker.json()["checks"]["clamav"]["status"] == "error"

    assert api.status_code == 200
    assert "clamav" not in api.json()["checks"]


async def test_the_clamav_probe_is_skipped_when_no_daemon_is_configured(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `SCANNER_BACKEND=structural` there is no daemon; reporting one down is noise."""
    _patch_all_ok(monkeypatch)

    def _structural() -> Settings:
        return Settings(scanner=ScannerSettings(backend="structural"))

    monkeypatch.setattr(health, "get_settings", _structural)
    monkeypatch.setattr(
        health, "check_clamav", _down("should not be called under SCANNER_BACKEND=structural")
    )

    resp = await probe_client.get("/health/ready/worker")

    assert resp.status_code == 200
    assert resp.json()["checks"]["clamav"]["status"] == "ok"
    assert "structural" in resp.json()["checks"]["clamav"]["error"]


async def test_ocr_down_takes_the_worker_probe_down_but_not_the_api(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-81(5)'s split, applied to the second sidecar (T-217, R-88).

    An OCR engine that is down must never evict the API, exactly as ClamAV must not. The
    asymmetry with ClamAV is the *worker* half: R-32 fails a job closed on an unreachable
    scanner, while R-88(9) makes recognition fail open, so what the red probe reports here is
    "scanned pages will ingest without their text", not "ingestion has stopped". It is still
    an operator's business, which is why it is a probe rather than a log line.
    """
    _patch_all_ok(monkeypatch)

    def _ocr_on() -> Settings:
        # See test_recognition.py: pinned so a developer's .env cannot trip the coupled
        # OCR + figures boot refusal inside a probe test.
        return Settings(parser=ParserSettings(ocr_enabled=True, figures_enabled=False))

    monkeypatch.setattr(health, "get_settings", _ocr_on)
    monkeypatch.setattr(health, "check_ocr", _down("ConnectionRefusedError"))

    worker = await probe_client.get("/health/ready/worker")
    api = await probe_client.get("/health/ready")

    assert worker.status_code == 503
    assert worker.json()["checks"]["ocr"]["status"] == "error"

    assert api.status_code == 200
    assert "ocr" not in api.json()["checks"]


async def test_the_ocr_probe_is_skipped_when_the_feature_is_off(
    probe_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-88(12) ships the feature off, and then there is no sidecar to report down."""
    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(
        health, "check_ocr", _down("should not be called under PARSER_OCR_ENABLED=false")
    )

    resp = await probe_client.get("/health/ready/worker")

    assert resp.status_code == 200
    assert resp.json()["checks"]["ocr"]["status"] == "ok"
    assert "PARSER_OCR_ENABLED=false" in resp.json()["checks"]["ocr"]["error"]


async def test_the_worker_probe_reads_the_arq_heartbeat_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the real probe body against a stubbed Redis client."""
    seen: list[str] = []

    class _Redis:
        async def get(self, key: str) -> bytes | None:
            seen.append(key)
            return b"Jul-28 16:00:00 j_complete=3 j_failed=0"

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(health.aioredis, "from_url", lambda *a, **k: _Redis())  # noqa: ARG005

    result = await health.check_worker()

    assert result.status == "ok"
    assert seen == [WORKER_HEALTH_CHECK_KEY]


async def test_the_worker_probe_fails_when_the_key_has_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arq sets a TTL of `heartbeat + 1`, so an absent key means nobody checked in."""

    class _Redis:
        async def get(self, key: str) -> bytes | None:
            return None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(health.aioredis, "from_url", lambda *a, **k: _Redis())  # noqa: ARG005

    result = await health.check_worker()

    assert result.status == "error"
    assert "heartbeat" in (result.error or "")


async def test_database_probe_against_local_db() -> None:
    """The real DB probe succeeds when the local ``corpus`` database is up (else skip)."""
    result = await health.check_database()
    if result.status == "error":
        pytest.skip(f"Postgres not reachable for readiness probe: {result.error}")
    assert result.status == "ok"
    assert result.latency_ms is not None
