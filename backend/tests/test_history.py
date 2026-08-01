"""Conversation-history rehydration (T-304, R-45(6), R-42(1)).

Split the way `test_fusion.py` is split from `test_retrieval.py`: the pure half — the role
mapping, the truncation bound, the import guard — is the *contract* three tasks depend on and
must not be skippable by a Postgres outage. The DB-backed half proves the ordering the
contract assumes.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository
from app.db.repositories.users import UserRepository
from app.rag import history as history_module
from app.rag.history import (
    HistoryTurn,
    load_history,
    load_router_tail,
    to_messages,
    to_prompt_history,
    truncate_turns,
)
from app.rag.prompts import compose_messages

TENANT_ID = uuid.UUID(int=0)


def _message(role: MessageRole, content: str) -> Message:
    return Message(conversation_id=uuid.uuid4(), role=role, content=content)


# --- the role mapping (the load-bearing part) ---------------------------------


def test_the_stored_ai_role_becomes_assistant() -> None:
    """`MessageRole.AI` is stored as ``"ai"``; a model payload needs ``"assistant"``.

    This is the whole reason the module exists. `prompts.compose_messages` keeps only
    `user`/`assistant` entries — silently, and correctly, since that guard is what stops a
    stray row becoming a second instruction channel. So a caller that passed ORM rows straight
    through would lose **every** assistant turn, with no error and no failing test: the model
    would receive a monologue of questions and answer the wrong one.
    """
    turns = to_prompt_history(
        [_message(MessageRole.USER, "how many tiers?"), _message(MessageRole.AI, "three")]
    )
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert [turn.content for turn in turns] == ["how many tiers?", "three"]


def test_every_message_role_is_mapped() -> None:
    """Adding a `MessageRole` member must fail here rather than vanish from every prompt."""
    assert set(history_module._ROLE_TO_PROMPT) == set(MessageRole)  # noqa: SLF001


def test_blank_content_is_dropped() -> None:
    """FR-MSG-08 Regenerate replaces `messages.content` in place; a momentarily blank turn
    contributes nothing but a confusing empty message."""
    turns = to_prompt_history(
        [_message(MessageRole.USER, "  "), _message(MessageRole.AI, "an answer")]
    )
    assert [turn.content for turn in turns] == ["an answer"]


# --- the truncation bound ------------------------------------------------------


def test_truncation_is_per_turn_not_over_the_concatenation() -> None:
    """A single long answer must not evict the user's own question.

    The question is the one turn a follow-up actually needs, so a budget spent on the
    concatenation — oldest-first — would drop exactly the wrong end.
    """
    turns = [
        HistoryTurn("assistant", "x" * 5_000),
        HistoryTurn("user", "what about the second one?"),
    ]
    capped = truncate_turns(turns, max_chars=100)

    assert len(capped) == 2
    assert capped[1].content == "what about the second one?"
    assert len(capped[0].content) <= 100 + len("[…] ")


def test_truncation_keeps_the_tail_of_a_turn() -> None:
    """ "The second one" refers to what was said *last*, so the tail is what carries meaning."""
    capped = truncate_turns([HistoryTurn("assistant", "alpha beta gamma delta")], max_chars=11)
    assert capped[0].content.endswith("gamma delta")
    assert "[…]" in capped[0].content, "a shortened turn must be visibly shortened"


def test_a_short_turn_is_untouched() -> None:
    turns = [HistoryTurn("user", "hello")]
    assert truncate_turns(turns, max_chars=100) == turns


# --- the composer hand-off (T-307) --------------------------------------------


def test_turns_reach_the_composer_as_mappings_it_keeps() -> None:
    """`to_messages` closes the last gap where a caller could hand-build the dicts.

    `HistoryTurn` is a dataclass and `compose_messages` reads `Mapping`s, so passing turns
    straight through would silently drop **every** history entry — the same failure shape as
    the `MessageRole.AI` trap above, one layer further on. Asserted end to end against the
    real composer rather than on the dicts alone, because "a mapping" is not the property that
    matters; "a mapping the composer keeps" is.
    """
    turns = to_prompt_history(
        [_message(MessageRole.USER, "how many tiers?"), _message(MessageRole.AI, "three")]
    )
    composed = compose_messages(query="and the third?", history=to_messages(turns))

    assert [message["role"] for message in composed.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert composed.messages[2]["content"] == "three"


# --- the import guard ----------------------------------------------------------


def test_history_module_imports_no_langgraph() -> None:
    """`app.rag.graph` calls `apply_strict_msgpack()` at import time, and T-402's route needs
    this module without that side effect — the `errors.py` / `prompts.py` reason."""
    text = Path(history_module.__file__).read_text(encoding="utf-8")
    for needle in ("langgraph", "langchain", "openai"):
        assert f"import {needle}" not in text and f"{needle} import" not in text, (
            f"app/rag/history.py imports `{needle}` — it must stay reachable without "
            "triggering `apply_strict_msgpack()` (R-45(6))"
        )


# --- ordering, against the database -------------------------------------------


async def _conversation(session: AsyncSession) -> Conversation:
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )
    conversation = Conversation(owner_id=user.id, tenant_id=TENANT_ID)
    session.add(conversation)
    await session.flush()
    return conversation


async def test_the_tail_is_the_last_n_messages_oldest_first(session: AsyncSession) -> None:
    """R-45(6): `ORDER BY seq DESC LIMIT n`, reversed — never `created_at` (T-108).

    A turn writes both rows in one transaction, so they share `created_at`, and any tiebreak
    on the random UUID id is a coin flip on whether the answer precedes the question.
    """
    conversation = await _conversation(session)
    for index in range(6):
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER if index % 2 == 0 else MessageRole.AI,
                content=f"turn {index}",
            )
        )
    await session.flush()

    tail = await load_router_tail(session, conversation.id, turns=3, max_chars=100)
    assert [turn.content for turn in tail] == ["turn 3", "turn 4", "turn 5"]
    assert [turn.role for turn in tail] == ["assistant", "user", "assistant"]

    full = await load_history(session, conversation.id)
    assert [turn.content for turn in full] == [f"turn {index}" for index in range(6)]


async def test_the_generation_read_stops_before_the_turn_it_answers(
    session: AsyncSession,
) -> None:
    """R-48(7). T-402 writes the user's message *before* the graph runs, and
    `compose_messages` appends the query itself — so an unbounded read asks the question
    twice, once before the fenced context and once after it.
    """
    conversation = await _conversation(session)
    rows = [
        Message(conversation_id=conversation.id, role=MessageRole.USER, content="how many tiers?"),
        Message(conversation_id=conversation.id, role=MessageRole.AI, content="three"),
        Message(conversation_id=conversation.id, role=MessageRole.USER, content="and the third?"),
    ]
    for row in rows:
        session.add(row)
    await session.flush()

    history = await load_history(session, conversation.id, until_message_id=rows[-1].id)
    assert [turn.content for turn in history] == ["how many tiers?", "three"]
    # Unbounded, the current question is in there twice over once the composer runs.
    assert len(await load_history(session, conversation.id)) == 3


async def test_a_message_id_from_another_conversation_yields_no_history(
    session: AsyncSession,
) -> None:
    """The safe direction, and it is worth pinning: the bound is a scalar subquery over *this*
    conversation, so a foreign id matches nothing and `seq < NULL` is false for every row.
    An absent history costs answer quality; a mis-scoped one would put another conversation's
    turns into this prompt.
    """
    mine = await _conversation(session)
    theirs = await _conversation(session)
    session.add(Message(conversation_id=mine.id, role=MessageRole.USER, content="mine"))
    stranger = Message(conversation_id=theirs.id, role=MessageRole.USER, content="theirs")
    session.add(stranger)
    await session.flush()

    assert await load_history(session, mine.id, until_message_id=stranger.id) == []
    assert await load_history(session, mine.id, until_message_id=uuid.uuid4()) == []


async def test_zero_turns_never_touches_the_database(session: AsyncSession) -> None:
    """`ROUTER_HISTORY_TURNS=0` must really cost nothing, not just return nothing."""
    conversation = await _conversation(session)
    session.add(
        Message(conversation_id=conversation.id, role=MessageRole.USER, content="a question")
    )
    await session.flush()

    assert await load_router_tail(session, conversation.id, turns=0, max_chars=100) == []
    assert await MessageRepository(session).list_tail(conversation.id, limit=0) == []
