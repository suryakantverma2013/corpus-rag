"""Scaffold smoke test: the app boots and the liveness probe responds (T-003)."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
