"""Feedback endpoint (T-403, FR-MSG-08, FR-MSG-06, OI-24, R-54, R-55).

Four groups of assertion here are load-bearing rather than routine:

* **The column, not just the echo.** Every store test refreshes the row and asserts the
  database value. A handler that simply echoed the request body would pass on the response
  alone, and feedback that never persists is exactly the defect nobody notices — nothing on
  any request path reads `messages.feedback` back (OI-24 defers its consumption).
* **The four `404`s share one string.** Absent, foreign, foreign-to-an-administrator and
  wrong-role are asserted individually *and* asserted identical. The identity is the actual
  NFR-SEC-02 property; without it the wrong-role branch becomes a probe for "this id exists
  and is mine".
* **The processing lock does not gate this route** (R-55). Decision 1 of the ruling is a
  comment until a published gate is observed not to refuse a `200`.
* **The event carries no payload text.** R-43(5)'s rule, asserted against the actual question
  and answer strings rather than against the key set — a future kwarg would slip past the
  latter.

Assertions are scoped to the caller each test mints (T-109): the suite runs against the
shared local database and nothing truncates it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.users import UserRepository
from app.rag import telemetry
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY
from app.services import chat as chat_service

pytestmark = pytest.mark.usefixtures("patch_jwks")

_QUESTION = "What is the refund window?"
_ANSWER = "Refunds are accepted within 30 days."


# ---- helpers ----


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _conversation(session: AsyncSession, *, owner_id: uuid.UUID) -> Conversation:
    conversation = Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    return conversation


async def _message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    role: MessageRole = MessageRole.AI,
    content: str = _ANSWER,
) -> Message:
    """One persisted turn half, with the citations envelope a real answer carries."""
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        citations={SEGMENTS_KEY: [{"text": content}], SOURCE_IDS_KEY: ["c1"]}
        if role is MessageRole.AI
        else None,
    )
    session.add(message)
    await session.flush()
    return message


async def _answer(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str], Conversation, Message]:
    owner, headers = await _caller(session, make_token, admin=admin)
    conversation = await _conversation(session, owner_id=owner)
    await _message(
        session, conversation_id=conversation.id, role=MessageRole.USER, content=_QUESTION
    )
    message = await _message(session, conversation_id=conversation.id)
    return owner, headers, conversation, message


def _url(message_id: uuid.UUID) -> str:
    return f"/api/v1/messages/{message_id}/feedback"


# ---- store and echo ----


@pytest.mark.parametrize(("wire", "stored"), [("up", Feedback.UP), ("down", Feedback.DOWN)])
async def test_a_thumb_is_stored_and_the_message_comes_back(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
    wire: str,
    stored: Feedback,
) -> None:
    """FR-MSG-08's 👍/👎, answered with FR-MSG-06's full message shape.

    The response assertions prove it is the *same* DTO the transcript serves — which is what
    lets the GUI's optimistic toggle reuse one type — and that re-serialising after the commit
    works (`expire_on_commit=False`). The refresh is what proves it was written at all.
    """
    _, headers, _, message = await _answer(session, make_token)

    response = await client.post(_url(message.id), json={"feedback": wire}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["feedback"] == wire
    assert body["id"] == str(message.id)
    assert body["role"] == "ai"
    assert body["segs"] == [{"text": _ANSWER}]
    assert body["created_at"]

    await session.refresh(message)
    assert message.feedback is stored


async def test_the_same_thumb_again_is_not_a_conflict(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-55(5) — the column is state, not an event log.

    Guarded explicitly so nobody later "improves" a repeat click into a `409`: the GUI cannot
    know the server's value is already `up` without a round trip it does not make.
    """
    _, headers, _, message = await _answer(session, make_token)

    first = await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)
    second = await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    assert (first.status_code, second.status_code) == (200, 200)
    await session.refresh(message)
    assert message.feedback is Feedback.UP


async def test_a_null_clears_the_rating(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """FR-MSG-06's third state — `feedback?: 'up'|'down'|null`.

    FR-MSG-08 says the control *toggles*, so a second click on the lit thumb has to be
    expressible. It clears to SQL `NULL`; `Feedback` has no `NONE` member and must not grow one.
    """
    _, headers, _, message = await _answer(session, make_token)
    await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    response = await client.post(_url(message.id), json={"feedback": None}, headers=headers)

    assert response.status_code == 200
    assert response.json()["feedback"] is None
    await session.refresh(message)
    assert message.feedback is None


# ---- request contract ----


async def test_an_empty_body_is_rejected_and_leaves_the_rating_alone(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-55(3) — the key is required, precisely so `{}` is not a silent erasure.

    The row assertion is what makes this a test about the missing default rather than a test
    about pydantic: with `feedback: Feedback | None = None`, this request would return `200`
    and quietly discard a rating the user gave.
    """
    _, headers, _, message = await _answer(session, make_token)
    await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    response = await client.post(_url(message.id), json={}, headers=headers)

    assert response.status_code == 422
    await session.refresh(message)
    assert message.feedback is Feedback.UP


async def test_a_value_outside_the_enum_is_rejected(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """The wire contract is the `Feedback` enum, and there is no third member to reach."""
    _, headers, _, message = await _answer(session, make_token)

    response = await client.post(_url(message.id), json={"feedback": "meh"}, headers=headers)

    assert response.status_code == 422


# ---- R-54 ownership, mirrored onto this surface ----


async def test_a_foreign_message_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-54 — ownership is the conversation's, applied in the query.

    The row assertion catches the write-then-check ordering: a handler that set the column
    before testing ownership would still answer `404` and would still have written.
    """
    _, _, _, message = await _answer(session, make_token)
    _, stranger = await _caller(session, make_token)

    response = await client.post(_url(message.id), json={"feedback": "up"}, headers=stranger)

    assert response.status_code == 404
    await session.refresh(message)
    assert message.feedback is None


async def test_an_administrator_gets_404_on_another_users_message(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-54(1) — no administrator widening, which is how OI-33 closed.

    The assertion a future "admins can see everything" change must break loudly. Rating someone
    else's answer would also write into a transcript §4.16 makes durable and exportable.
    """
    _, _, _, message = await _answer(session, make_token)
    _, admin = await _caller(session, make_token, admin=True)

    response = await client.post(_url(message.id), json={"feedback": "up"}, headers=admin)

    assert response.status_code == 404


async def test_an_unknown_message_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """Also the answer for an errored turn: R-54(3) persists no row, so there is none to rate."""
    _, headers = await _caller(session, make_token)

    response = await client.post(_url(uuid.uuid4()), json={"feedback": "up"}, headers=headers)

    assert response.status_code == 404


async def test_the_callers_own_question_cannot_be_rated(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-55(2) — FR-MSG-04 puts the action bar beneath AI answers only.

    A `404` rather than the `409` this surface uses for `NotRetryableError`: those answer a
    request a *correct* client could make against state that moved under it, and no correct
    client rates its own question.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    question = await _message(
        session, conversation_id=conversation.id, role=MessageRole.USER, content=_QUESTION
    )

    response = await client.post(_url(question.id), json={"feedback": "up"}, headers=headers)

    assert response.status_code == 404
    await session.refresh(question)
    assert question.feedback is None


async def test_every_refusal_answers_with_the_same_copy(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """NFR-SEC-02, and the property the four tests above only approach one at a time.

    Absent, foreign, foreign-to-an-admin and wrong-role must be indistinguishable in the
    response, or the route becomes a probe for which message ids exist and whose they are.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    question = await _message(
        session, conversation_id=conversation.id, role=MessageRole.USER, content=_QUESTION
    )
    _, _, _, foreign = await _answer(session, make_token)
    _, admin = await _caller(session, make_token, admin=True)

    details = {
        (await client.post(url, json={"feedback": "up"}, headers=caller)).json()["detail"]
        for url, caller in (
            (_url(uuid.uuid4()), headers),
            (_url(foreign.id), headers),
            (_url(foreign.id), admin),
            (_url(question.id), headers),
        )
    }

    assert len(details) == 1, details


# ---- R-55(1): the processing lock does not gate this route ----


async def test_feedback_is_not_gated_by_the_processing_lock(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-55(1), and the only thing that makes that decision regressable.

    FR-MSG-08 names the processing lock, so the temptation to reuse
    `_require_no_active_processing` here is real. R-43(4) gates exactly R-24's four *file*
    verbs: the answer being generated has no row yet and so can never be this route's target,
    and because the gate is keyed on the **caller** — not the conversation — enforcing it would
    refuse feedback on one message because a *different* chat is mid-turn.
    """
    owner, headers, _, message = await _answer(session, make_token)
    session.add(
        ProcessingLock(
            owner_id=owner,
            conversation_id=None,
            token=uuid.uuid4().hex,
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
    )
    await session.flush()

    response = await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    assert response.status_code == 200
    await session.refresh(message)
    assert message.feedback is Feedback.UP


# ---- OI-24's export half ----


async def test_the_write_is_exported_to_the_log_stream(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """OI-24 / NFR-OBS-05 — there is no sink yet, so the deliverable is the event (R-55).

    T-604 binds a durable record to this name. The payload-text assertions are R-43(5)'s rule
    and are made against the actual question and answer strings rather than against the key
    set: a future kwarg carrying text would slip past a key-set check.
    """
    owner, headers, conversation, message = await _answer(session, make_token)

    with structlog.testing.capture_logs() as logs:
        response = await client.post(_url(message.id), json={"feedback": "down"}, headers=headers)

    assert response.status_code == 200
    entry = next(e for e in logs if e["event"] == chat_service.FEEDBACK_RECORDED)
    assert entry["message_id"] == str(message.id)
    assert entry["conversation_id"] == str(conversation.id)
    assert entry["owner_id"] == str(owner)
    assert entry["feedback"] == "down"
    assert _ANSWER not in str(logs), "R-43(5) — no payload text, ever"
    assert _QUESTION not in str(logs), "R-43(5) — no payload text, ever"


async def test_clearing_is_exported_as_an_explicit_null(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """ "Cleared" and "never rated" are different facts, so the key is present and null.

    OI-24's calibration loop is the consumer that would have to tell them apart; omitting the
    key, or emitting `"none"`, loses that distinction where nothing would ever notice.
    """
    _, headers, _, message = await _answer(session, make_token)
    await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    with structlog.testing.capture_logs() as logs:
        await client.post(_url(message.id), json={"feedback": None}, headers=headers)

    entry = next(e for e in logs if e["event"] == chat_service.FEEDBACK_RECORDED)
    assert "feedback" in entry
    assert entry["feedback"] is None


async def test_nothing_is_exported_when_the_write_does_not_commit(
    client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_token,  # noqa: ANN001
) -> None:
    """The event is logged **after** the commit — `conversation.deleted`'s rule.

    A logged event must correspond to durable state. Emitting it first would put a rating in
    T-604's sink that the database never received, and since nothing on any request path reads
    `messages.feedback` back, the two would never be reconciled.
    """
    _, headers, _, message = await _answer(session, make_token)

    async def _boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", _boom)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError):
            await client.post(_url(message.id), json={"feedback": "up"}, headers=headers)

    assert not [e for e in logs if e["event"] == chat_service.FEEDBACK_RECORDED]


def test_the_feedback_event_stays_outside_the_closed_turn_vocabulary() -> None:
    """R-43(5) — `graph.turn.*` is a closed set whose members pair as spans.

    Feedback is a user action on a turn that already ended; an event inside that set would
    leave T-604's sink pairing a `.start` that never happened.
    """
    assert chat_service.FEEDBACK_RECORDED not in telemetry.EVENT_NAMES
    assert not chat_service.FEEDBACK_RECORDED.startswith("graph.turn.")


# ---- T-405's contract ----
#
# `test_the_feedback_key_is_required_in_the_openapi_schema` **moved** to
# `tests/test_openapi_contract.py` when T-405 landed, and the move is the point: it ran on the
# `app` fixture here, which is DB-gated and *skips* when Postgres is unreachable — so the one
# assertion protecting R-55(4)'s required key would have stopped running without saying so.
# It now uses the DB-free `openapi_document` fixture, beside the rest of the schema contract.
