"""Conversation-history rehydration (T-304, R-45(6), R-42(1)).

R-42(1) satisfies FR-PER-01's "message history used by the graph" **by reference**: the
checkpoint carries `user_message_id`/`turn_index`, and the transcript itself is
``messages WHERE conversation_id = thread_id ORDER BY seq`` — the copy OI-23 already makes
authoritative for display. This module is that read, in one place, because three tasks need
it and they must not each roll their own:

* T-304's router sees a **bounded tail** (`ROUTER_HISTORY_*`), enough to resolve "what about
  the second one?" without paying for a full-history call on a classification.
* T-307's generator sees the **whole** history, untruncated, per R-30 — the 10.4K budget
  counts history + query and FR-STA-04's warn-and-block is what keeps it in range, so there
  is no windowing to do. It does stop **short of the turn it is answering** (R-48(7)): that
  row is already in `messages` before the graph starts, and the composer appends the query
  itself, so an unbounded read would ask the question twice.
* T-402 lists the same rows for the API.

**The role mapping is the load-bearing part.** `MessageRole.AI` is stored as ``"ai"``, and
:func:`app.rag.prompts.compose_messages` keeps only ``user``/``assistant`` entries —
silently, and correctly so, since that guard is what stops a stray row becoming a second
instruction channel. A caller that passed raw rows through would therefore lose **every**
assistant turn with no error anywhere: the model would see a monologue of user questions and
answer the wrong one. Hence :func:`to_prompt_history`, and hence no caller builds these
dicts by hand.

**Imports no langgraph**, for the reason `app.rag.errors` and `app.rag.prompts` do not:
`app.rag.graph` calls ``apply_strict_msgpack()`` at import time, and T-402's route must be
able to reach this module without triggering it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MessageRole
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository

__all__ = [
    "HistoryTurn",
    "load_history",
    "load_router_tail",
    "to_messages",
    "to_prompt_history",
    "truncate_turns",
]

#: What `role` becomes in a model payload. `MessageRole.USER` already matches, but both are
#: mapped explicitly so adding a third `MessageRole` member fails here — visibly — rather
#: than silently dropping that role out of every prompt.
_ROLE_TO_PROMPT: dict[MessageRole, str] = {
    MessageRole.USER: "user",
    MessageRole.AI: "assistant",
}

#: Appended to a turn cut by :func:`truncate_turns`, so the model can tell a shortened turn
#: from one that ended there. Cheap, and it stops a truncated question reading as a complete
#: (different) one.
_ELLIPSIS = " […]"


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One prior message, reduced to what a prompt can use.

    Deliberately not the ORM row: this crosses into `app.rag.prompts`, which knows nothing
    about the database, and a detached `Message` would carry a session-bound identity into a
    layer that has no session.
    """

    role: str
    content: str


def to_prompt_history(messages: Iterable[Message]) -> list[HistoryTurn]:
    """Map ORM rows to prompt turns, dropping anything with no usable role or content.

    An empty `content` is dropped rather than passed through: FR-MSG-08 Regenerate replaces
    `messages.content` in place, and a turn that is momentarily blank contributes nothing but
    a confusing empty message.
    """
    turns: list[HistoryTurn] = []
    for message in messages:
        role = _ROLE_TO_PROMPT.get(message.role)
        if role is None or not (message.content or "").strip():
            continue
        turns.append(HistoryTurn(role=role, content=message.content))
    return turns


def to_messages(turns: Iterable[HistoryTurn]) -> list[dict[str, str]]:
    """Prompt turns → the mapping shape :func:`app.rag.prompts.compose_messages` reads.

    :class:`HistoryTurn` is a dataclass and the composer reads ``Mapping``s, so without this
    every caller would hand-build the dicts — which is exactly the step this module exists to
    own. Kept here rather than as a method so the conversion sits beside the role mapping it
    depends on.
    """
    return [{"role": turn.role, "content": turn.content} for turn in turns]


async def load_history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    until_message_id: uuid.UUID | None = None,
) -> list[HistoryTurn]:
    """The full transcript, oldest first (R-30, R-42(1)) — T-307's input.

    Untruncated by design: R-30 counts history + query against the 10.4K budget and FR-STA-04
    warns and blocks before it is exceeded, so there is no windowing to do here.

    ``until_message_id`` bounds the read *below* that row (R-48(7)). T-307 passes
    `RAGState.user_message_id`, because the row this turn answers is already in the table by
    the time the graph runs and `compose_messages` appends the query itself — see
    :meth:`app.db.repositories.messages.MessageRepository.list_before`.
    """
    repository = MessageRepository(session)
    rows = (
        await repository.list_before(conversation_id, message_id=until_message_id)
        if until_message_id is not None
        else await repository.list_by_conversation(conversation_id)
    )
    return to_prompt_history(rows)


async def load_router_tail(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    turns: int,
    max_chars: int,
) -> list[HistoryTurn]:
    """The last ``turns`` messages, each truncated to ``max_chars`` (R-45(6)).

    Reads only the tail from the database rather than loading the transcript and slicing it:
    a long conversation is exactly the case where the router's cheap classification must not
    become the most expensive query of the turn.

    ``turns == 0`` short-circuits without touching the database, so setting
    `ROUTER_HISTORY_TURNS=0` really does cost nothing.
    """
    if turns <= 0:
        return []
    rows = await MessageRepository(session).list_tail(conversation_id, limit=turns)
    return truncate_turns(to_prompt_history(rows), max_chars=max_chars)


def truncate_turns(turns: Sequence[HistoryTurn], *, max_chars: int) -> list[HistoryTurn]:
    """Cap each turn's content. Pure, so the bound is testable without a database.

    Truncation is **per turn**, not over the concatenation: a single long answer would
    otherwise consume the whole budget and evict the user's own question, which is the one
    turn a follow-up needs.

    The **tail** of a turn is kept, not the head. For an assistant answer the last sentences
    are what a follow-up refers to ("the second one"), and for a user question the operative
    clause is far more often at the end than at the start.
    """
    if max_chars < 1:
        return [HistoryTurn(role=turn.role, content="") for turn in turns]
    capped: list[HistoryTurn] = []
    for turn in turns:
        content = turn.content
        if len(content) > max_chars:
            content = _ELLIPSIS.strip() + " " + content[-max_chars:]
        capped.append(HistoryTurn(role=turn.role, content=content))
    return capped
