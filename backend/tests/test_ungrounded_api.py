"""`POST /messages/{id}/general-knowledge` — FR-MSG-09's route (R-98, T-727).

The service's own properties are asserted in `test_ungrounded.py`; this file is about the
**route contract**, and three of its assertions are the ones that would otherwise rot:

* **The four refusals share the `404`'s copy.** Absent, foreign, foreign-to-an-administrator
  and non-AI are asserted individually *and* asserted identical, because the identity is the
  NFR-SEC-02 property — without it the route becomes a probe for "this id exists and is mine".
* **It appends rather than replaces.** The transcript must still contain the abstention
  afterwards; this is the deliberate opposite of Regenerate (R-56), and the difference is
  invisible in the response body alone.
* **The response carries no citations.** The wire shape is what the GUI branches on, so an
  answer that arrived looking cited would render as grounded whatever the column says.

Scoped to the caller each test mints (T-109): the suite runs against the shared local database
and nothing truncates it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.users import UserRepository
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY

pytestmark = pytest.mark.usefixtures("patch_jwks")

_QUESTION = "What are the principles of mathematical induction?"
_ABSTENTION = "I couldn't ground an answer to that in your documents"


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _abstained_turn(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[dict[str, str], Conversation, Message]:
    """A question and the refusal it drew. The refusal cites nothing — that is what makes it one."""
    owner, headers = await _caller(session, make_token, admin=admin)
    conversation = Conversation(owner_id=owner, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role=MessageRole.USER, content=_QUESTION)
    )
    target = Message(
        conversation_id=conversation.id, role=MessageRole.AI, content=_ABSTENTION, citations=None
    )
    session.add(target)
    await session.flush()
    await session.commit()
    return headers, conversation, target


def _url(message_id: uuid.UUID) -> str:
    return f"/api/v1/messages/{message_id}/general-knowledge"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setattr(get_settings().ungrounded, "fallback_enabled", True)


# ---- the happy path ----


async def test_it_appends_an_uncited_answer_and_leaves_the_abstention(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    enabled,  # noqa: ANN001
) -> None:
    """R-98(1)/(3): the two things the response alone could hide.

    The abstention must survive — it is the record that the corpus could not answer, which is
    the reason an automatic fallback is declined — and the new answer must carry no citations
    on the **wire**, since that is what the GUI branches on.
    """
    headers, conversation, target = await _abstained_turn(session, make_token)

    resp = await client.post(_url(target.id), headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] != str(target.id), "it appends; it must not overwrite"
    assert not [s for s in body["segs"] if s.get("isCite")], "an ungrounded answer cites nothing"

    rows = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.seq)
        )
    ).all()
    assert [r.content for r in rows][:2] == [_QUESTION, _ABSTENTION]
    assert rows[-1].ungrounded is True
    assert len(rows) == 3


# ---- refusals ----


async def test_it_is_refused_when_the_deployment_has_not_enabled_it(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """No `enabled` fixture: the default is off (R-98(2)), and the route must honour it.

    If only the GUI hid the control, any client could still ask a deployment that never opted
    in to produce ungrounded text.
    """
    headers, _conversation, target = await _abstained_turn(session, make_token)

    resp = await client.post(_url(target.id), headers=headers)

    assert resp.status_code == 409


async def test_a_grounded_answer_cannot_be_fallen_back_from(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    enabled,  # noqa: ANN001
) -> None:
    """FR-MSG-09 offers the control on an abstention only."""
    owner, headers = await _caller(session, make_token)
    conversation = Conversation(owner_id=owner, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    answered = Message(
        conversation_id=conversation.id,
        role=MessageRole.AI,
        content="Grounded.",
        citations={SEGMENTS_KEY: [{"text": "Grounded."}], SOURCE_IDS_KEY: ["c1"]},
    )
    session.add(answered)
    await session.commit()

    resp = await client.post(_url(answered.id), headers=headers)

    assert resp.status_code == 409


async def test_the_four_not_found_cases_are_one_indistinguishable_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    enabled,  # noqa: ANN001
) -> None:
    """R-55(2) and R-54(1). The *identity* is the property, not the status.

    Distinguishing them would make the route a probe for which ids exist and whose they are —
    and an administrator gets the same `404` on a foreign message, because R-54 gives no
    widening on conversation-owned data.
    """
    _headers, _conversation, target = await _abstained_turn(session, make_token)
    _, stranger = await _caller(session, make_token)
    _, admin = await _caller(session, make_token, admin=True)
    owner, mine = await _caller(session, make_token)
    my_chat = Conversation(owner_id=owner, tenant_id=DEFAULT_TENANT_ID, title="Mine")
    session.add(my_chat)
    await session.flush()
    question = Message(
        conversation_id=my_chat.id, role=MessageRole.USER, content="not an answer"
    )
    session.add(question)
    await session.commit()

    responses = [
        await client.post(_url(uuid.uuid4()), headers=mine),      # absent
        await client.post(_url(target.id), headers=stranger),      # foreign
        await client.post(_url(target.id), headers=admin),         # foreign, to an admin
        await client.post(_url(question.id), headers=mine),        # mine, but not an AI answer
    ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert len({r.json()["detail"] for r in responses}) == 1, (
        "telling them apart turns the route into an existence probe"
    )


async def test_it_requires_authentication(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _headers, _conversation, target = await _abstained_turn(session, make_token)
    assert (await client.post(_url(target.id))).status_code == 401
