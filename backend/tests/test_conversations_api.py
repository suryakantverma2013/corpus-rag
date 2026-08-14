"""Conversations API (T-401, FR-SBR-02/03/04/07, FR-PER-02, R-42(11), R-54).

Two things here are not ordinary CRUD assertions and are the reason the file exists:

* **The delete purges the LangGraph thread, and purges it *before* it commits.** Both halves
  are asserted, the ordering by making the purge fail and checking the conversation survived.
  Getting that backwards leaves a thread nobody can reach and nothing sweeps (OI-30) — a
  privacy defect that no amount of later testing would surface, because the evidence is in a
  table the application never reads again.
* **A foreign conversation is `404` for an administrator too** (R-54). That is the whole of
  OI-33's closure and the one assertion a future "admins can see everything" change would
  break loudly rather than quietly.

Assertions are scoped to the caller each test mints (T-109): the suite runs against the
shared local database and nothing truncates it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.users import UserRepository
from app.services import conversations as conversations_service

pytestmark = pytest.mark.usefixtures("patch_jwks")


# ---- helpers ----


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _conversation(
    session: AsyncSession, *, owner_id: uuid.UUID, title: str | None = "A chat"
) -> Conversation:
    conversation = Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title=title)
    session.add(conversation)
    await session.flush()
    return conversation


class _FakeSaver:
    """The one method `delete_conversation` calls on the checkpointer."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(thread_id)


# ---- CRUD ----


async def test_create_returns_the_new_chat_with_an_empty_context_meter(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-SBR-02 plus R-51's binding: the FR-ANL-03 meter ships on the conversation.

    A new chat starts at **zero**, which is OI-16 as ratified — the budget is per-chat, so
    starting a new one is the escape from a full one (the FR-STA-04 block's whole premise).
    """
    _, headers = await _caller(session, make_token)

    response = await client.post(
        "/api/v1/conversations", json={"title": "Q3 review"}, headers=headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Q3 review"
    assert body["archived"] is False
    assert body["context"]["used_tokens"] == 0
    assert body["context"]["limit_tokens"] > 0
    assert body["context"]["remaining_tokens"] == body["context"]["limit_tokens"]
    assert body["message_count"] == 0


async def test_the_meter_publishes_the_answer_reserve_the_server_checks_with(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """T-513/§8.56(4) — without this the GUI cannot reproduce `check_submission`.

    The composer blocks *before* a request exists (R-51(6)), so it must project
    `used + query + reserve` itself. Asserted against the live setting rather than a literal:
    a hard-coded 1,500 here would keep passing while an operator raised
    `LLM_MAX_OUTPUT_TOKENS` and the browser started accepting what the server refuses.
    """
    from app.config import get_settings

    _, headers = await _caller(session, make_token)

    response = await client.post("/api/v1/conversations", json={}, headers=headers)

    assert response.status_code == 201
    reserve = response.json()["context"]["answer_reserve_tokens"]
    assert reserve == get_settings().context.answer_reserve_tokens
    assert reserve > 0


async def test_the_context_meter_counts_the_transcript_not_the_model_s_usage(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-51(1) — the card measures *conversation length*.

    `prompt_tokens` on the row is deliberately absurd here: it covers the system prompt and
    the retrieved chunks as well, and the meter must not read it. If these two ever agree,
    someone has wired the card to the model's own count.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content="x" * 400,
            prompt_tokens=99_999,
        )
    )
    await session.flush()

    response = await client.get(f"/api/v1/conversations/{conversation.id}", headers=headers)

    assert response.status_code == 200
    used = response.json()["context"]["used_tokens"]
    assert used == 100, "ceil(400/4) — the shared app.tokens estimate, per message"


async def test_list_is_sidebar_ordered_and_scoped_to_the_caller(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-SBR-03 — most recently updated first, and only the caller's own chats."""
    owner, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    older = await _conversation(session, owner_id=owner, title="older")
    newer = await _conversation(session, owner_id=owner, title="newer")
    foreign = await _conversation(session, owner_id=stranger, title="not yours")
    # `clock_timestamp()` advances mid-transaction (T-108), so the two are genuinely ordered.
    assert newer.updated_at > older.updated_at

    response = await client.get("/api/v1/conversations", headers=headers)

    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids[:2] == [str(newer.id), str(older.id)]
    assert str(foreign.id) not in ids


async def test_every_list_row_carries_its_own_message_count(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """T-407 — FR-SBR-03's "· N messages", on rows the client never opens.

    The counts differ per row *and* a stranger's messages sit in the same table, so a
    subquery that lost its correlation would answer the same (wrong) number everywhere.
    """
    owner, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    empty = await _conversation(session, owner_id=owner, title="empty")
    busy = await _conversation(session, owner_id=owner, title="busy")
    noisy = await _conversation(session, owner_id=stranger, title="not yours")
    for conversation, count in ((busy, 3), (noisy, 7)):
        for _ in range(count):
            session.add(
                Message(conversation_id=conversation.id, role=MessageRole.USER, content="hi")
            )
    await session.flush()

    response = await client.get("/api/v1/conversations", headers=headers)

    rows = {row["id"]: row["message_count"] for row in response.json()}
    assert rows[str(busy.id)] == 3
    assert rows[str(empty.id)] == 0
    assert str(noisy.id) not in rows


async def test_the_detail_routes_carry_the_count_too(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The field is total, never optional — a row that sometimes has a count would render
    `· 0 messages` for a chat that has two, which is worse than blank because it is plausible."""
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    for _ in range(2):
        session.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content="hi"))
    await session.flush()

    response = await client.get(f"/api/v1/conversations/{conversation.id}", headers=headers)

    assert response.json()["message_count"] == 2


async def test_rename_updates_the_title_and_the_sidebar_order(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-SBR-04. `updated_at` has to move or the renamed chat does not rise in the sidebar."""
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner, title="untitled")
    before = conversation.updated_at

    response = await client.patch(
        f"/api/v1/conversations/{conversation.id}", json={"title": "  Renamed  "}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Renamed", "the title is stripped, not stored padded"
    await session.refresh(conversation)
    assert conversation.updated_at > before


async def test_a_blank_rename_is_rejected(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Whitespace is not a title — it would render as an unlabelled row in the sidebar."""
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    response = await client.patch(
        f"/api/v1/conversations/{conversation.id}", json={"title": "   "}, headers=headers
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("patch", ""), ("delete", "")],
)
async def test_a_foreign_conversation_is_404_not_403(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    method: str,
    suffix: str,
) -> None:
    """NFR-SEC-02 — the response must not confirm the id exists."""
    stranger, _ = await _caller(session, make_token)
    _, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=stranger)

    call = getattr(client, method)
    kwargs = {"headers": headers}
    if method == "patch":
        kwargs["json"] = {"title": "mine now"}
    response = await call(f"/api/v1/conversations/{conversation.id}{suffix}", **kwargs)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_an_administrator_gets_404_on_another_users_chat(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """**R-54, closing OI-33.** There is no administrator widening on this surface.

    FR-USR-06/R-39(1) make *document* deletion owner-or-admin, and it would be easy to
    generalise that to transcripts. Nothing in the spec asks for it, and the cost is concrete:
    an admin turn on a foreign chat writes a message into an owner's durable transcript
    (§4.16), which the FR-ANL cards then count. Refusing the read closes the question.
    """
    stranger, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    conversation = await _conversation(session, owner_id=stranger)

    response = await client.get(f"/api/v1/conversations/{conversation.id}", headers=admin_headers)

    assert response.status_code == 404


# ---- delete (R-42(11)) ----


async def test_delete_removes_the_transcript_and_purges_the_graph_thread(
    session: AsyncSession,
) -> None:
    """R-42(11) — FR-SBR-07 must reach the checkpointer, which holds no FK to cascade on.

    The thread id **is** the conversation id (FR-PER-02), asserted rather than assumed:
    purging a thread named anything else leaves the real one behind while looking correct.
    """
    owner = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=owner, email=f"{owner.hex[:8]}@corpus.local", display_name="User"
    )
    conversation = await _conversation(session, owner_id=owner)
    session.add(
        Message(conversation_id=conversation.id, role=MessageRole.USER, content="a question")
    )
    await session.flush()
    saver = _FakeSaver()

    await conversations_service.delete_conversation(session, conversation, saver=saver)

    assert saver.deleted == [str(conversation.id)]
    assert await session.get(Conversation, conversation.id) is None
    remaining = await session.scalars(
        select(Message).where(Message.conversation_id == conversation.id)
    )
    assert list(remaining) == []


async def test_a_failed_thread_purge_leaves_the_conversation_intact(
    session: AsyncSession,
) -> None:
    """The purge happens **before** the commit, and this is what that buys.

    Mirror of R-39(9)'s purge-before-commit for object storage. Committed first, a failure
    here would leave graph state for a conversation no request can name again — and OI-30
    records that nothing sweeps it, so it would stay for ever. Failing first leaves a chat the
    user can simply delete again.
    """
    owner = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=owner, email=f"{owner.hex[:8]}@corpus.local", display_name="User"
    )
    conversation = await _conversation(session, owner_id=owner)
    conversation_id = conversation.id
    # Committed before the attempt, or the rollback below would discard the seed along with
    # the delete and the assertion would pass for the wrong reason. (`conftest`'s outer
    # transaction still rolls the whole test back.) In production this row was committed by
    # an earlier request, which is what the commit stands in for.
    await session.commit()
    saver = _FakeSaver(error=RuntimeError("checkpointer down"))

    with pytest.raises(conversations_service.ThreadDeletionError):
        await conversations_service.delete_conversation(session, conversation, saver=saver)

    await session.rollback()
    assert await session.get(Conversation, conversation_id) is not None


async def test_delete_answers_503_when_the_thread_cannot_be_purged(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`503`, not `500`: unreachable is transient and the caller's retry is meaningful.

    The narrowing T-110 made to the Keycloak surface, applied here — a status a proxy and a
    retrying client both act on must mean what it says.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    await session.commit()  # see the sibling test — the route rolls back on this path
    # Held as a plain UUID: the route's rollback expires the instance, and reading `.id` off
    # it afterwards would trigger a lazy reload outside an await.
    conversation_id = conversation.id

    async def _boom(*_: object, **__: object) -> None:
        raise conversations_service.ThreadDeletionError(str(conversation_id))

    monkeypatch.setattr(conversations_service, "delete_conversation", _boom)

    response = await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)

    assert response.status_code == 503
    assert await session.get(Conversation, conversation_id) is not None
