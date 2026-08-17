"""`GET /api/v1/config` — FR-SYS-03's configured model id (T-513).

Three assertions and each is the reason the route exists rather than a field on `MeResponse`
or a constant in the GUI: it reports the *configured* value, it **follows** that configuration,
and it is not public.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.usefixtures("patch_jwks")

_URL = "/api/v1/config"


async def _caller(
    session: AsyncSession, make_token: Callable[..., str]
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=('user',))}"}


async def test_it_reports_the_configured_chat_model(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-ANL-02's MODEL card. Compared against the live setting, never a literal — a
    hard-coded `gpt-4o` here would be the same silently-drifting copy the route removes."""
    _, headers = await _caller(session, make_token)

    response = await client.get(_URL, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"chat_model": get_settings().openai.chat_model}


async def test_it_follows_the_configuration_rather_than_restating_it(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: an operator changing `OPENAI_CHAT_MODEL` changes what the card shows.

    A route returning a constant would pass the test above and fail this one.
    """
    _, headers = await _caller(session, make_token)
    monkeypatch.setattr(get_settings().openai, "chat_model", "gpt-4o-mini-2026-01-01")

    response = await client.get(_URL, headers=headers)

    assert response.json()["chat_model"] == "gpt-4o-mini-2026-01-01"


async def test_it_is_not_public(client: httpx.AsyncClient) -> None:
    """Internal product configuration, not a discovery document. The `CurrentUser` dependency
    is the only thing enforcing that, and it is easy to drop while everything still works."""
    assert (await client.get(_URL)).status_code == 401


async def test_it_reports_the_operators_override_rather_than_the_environment(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """T-611/R-83: since the answer model is repointable at runtime, the *configured* value
    and the value **in force** can differ — and this card must name the one that will answer.

    Reading `settings.openai.chat_model` here would pass every other test in this file and
    reintroduce, one layer down, exactly the silent drift this route was added to remove.
    """
    from app.services.model_selection import ModelSlot, set_model_override

    _, headers = await _caller(session, make_token)
    await set_model_override(
        session, slot=ModelSlot.CHAT, model_id="gpt-5-preview", updated_by="test"
    )

    response = await client.get(_URL, headers=headers)

    assert response.status_code == 200
    assert response.json()["chat_model"] == "gpt-5-preview"
    assert get_settings().openai.chat_model != "gpt-5-preview", (
        "the fixture must differ from the environment or the assertion above is vacuous"
    )
