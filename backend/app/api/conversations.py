"""``/conversations`` routes — chat lifecycle (T-401, FR-SBR-02/03/04/07, FR-PER-02/04).

The R-25 API baseline (Source A §6) spells these five verbs, and they are thin: the ordering
is a repository predicate and the deletion ordering lives in `app.services.conversations`.

**Every route is owner-scoped, and a foreign id is `404`.** Not `403`, which would confirm
the id exists (NFR-SEC-02, the convention `documents.py` and `jobs.py` already follow), and
**no administrator widening** — that is R-54, which closes OI-33 by making admin chat
impersonation unreachable rather than by ruling on what it would retrieve. FR-USR-06/R-39(1)
make *document* deletion owner-or-admin; nothing asks for the same over a transcript.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, DbSession, SettingsDep
from app.config import Settings
from app.db.models.conversation import Conversation
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.messages import MessageRepository
from app.rag.budget import ContextUsage, conversation_usage
from app.services import conversations as conversations_service
from app.services.conversations import ThreadDeletionError

router = APIRouter(prefix="/conversations", tags=["conversations"])

_NOT_FOUND = "Conversation not found."

#: A conversation whose thread could not be purged is **not** deleted (see
#: `delete_conversation`). `503` rather than `500`: the checkpointer being unreachable is
#: transient and the caller's retry is meaningful — the same reading R-29 gives readiness and
#: T-110 narrowed 503 down to.
_CHECKPOINTER_UNAVAILABLE = (  # TBD(§8.4)
    "Couldn't delete this chat just now. Please try again shortly."
)

_TITLE_MAX = 500  # matches `conversations.title`


class ContextWindowResponse(BaseModel):
    """The FR-ANL-03 CONTEXT WINDOW card, for one conversation (R-51 binds T-401 to carry it).

    **This is conversation length, not model cost.** R-30 counts history + the current query
    and excludes the system prompt and the retrieved chunks, so a turn's real
    `messages.prompt_tokens` will routinely exceed `used_tokens` — correctly, because they
    measure different quantities (R-51(1)). Nothing may derive one from the other.
    """

    used_tokens: int
    limit_tokens: int
    remaining_tokens: int
    percent_used: float
    answer_reserve_tokens: int = Field(
        description=(
            "The headroom FR-STA-04's projection reserves for the reply "
            "(`CONTEXT_ANSWER_RESERVE_TOKENS`, floored at `LLM_MAX_OUTPUT_TOKENS`). Published "
            "so the GUI can reproduce the server's admission decision *before* a request "
            "exists (R-51(3)/(6)) — without it the composer accepts what the server refuses "
            "the moment an operator raises the answer ceiling."
        )
    )

    @classmethod
    def of(cls, usage: ContextUsage) -> ContextWindowResponse:
        """A pure projection of :class:`~app.rag.budget.ContextUsage`.

        Deliberately takes no `Settings`: the reserve is read off the same object
        `check_submission` reads it from, so the number published here cannot drift from the
        number the admission check used.
        """
        return cls(
            used_tokens=usage.used_tokens,
            limit_tokens=usage.limit_tokens,
            remaining_tokens=usage.remaining_tokens,
            percent_used=usage.percent_used,
            answer_reserve_tokens=usage.answer_reserve_tokens,
        )


class ConversationResponse(BaseModel):
    """One conversation. `id` is also the LangGraph `thread_id` (FR-PER-02)."""

    id: uuid.UUID
    title: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(
        description=(
            "How many messages this chat holds — FR-SBR-03's `· N messages`, on every row "
            "including the ones the sidebar never opens."
        )
    )
    """FR-SBR-03's per-row count (T-407).

    **This is not a crack in the rule that keeps `context` off the list route.** That omission
    is R-51(4): the meter is derived by reading every `messages.content` in the chat, so a list
    would read every transcript the caller owns. A `COUNT(*)` over an indexed foreign key reads
    **no content at all**, so the argument does not carry over — which is worth saying here,
    because the next reader will assume it does.

    Total rather than optional: a row that sometimes has a count and sometimes does not would
    make the sidebar render `· 0 messages` for a chat that has ten, which is worse than blank
    because it is plausible.
    """


class ConversationDetailResponse(ConversationResponse):
    """A single conversation, with the FR-ANL-03 meter.

    Carried on the single-conversation responses only. The list route omits it deliberately:
    the meter is derived from every `messages.content` in the chat (R-51(4) — usage is
    computed, never stored), so putting it on a list would read the full transcript of every
    conversation in the sidebar to render a card FR-ANL-03 shows for the *active* one.

    `message_count`, inherited from the base, is the one count that *is* on the list route —
    see its docstring for why the same objection does not apply to it.
    """

    context: ContextWindowResponse


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=_TITLE_MAX)


class RenameConversationRequest(BaseModel):
    """FR-SBR-04. `title` is required — this route renames and does nothing else."""

    title: str = Field(min_length=1, max_length=_TITLE_MAX)

    @field_validator("title")
    @classmethod
    def _stripped(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title must not be blank")
        return title


def _to_response(conversation: Conversation, message_count: int) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=message_count,
    )


async def _detail(
    session: AsyncSession, conversation: Conversation, settings: Settings
) -> ConversationDetailResponse:
    usage = await conversation_usage(session, conversation.id, settings=settings)
    # A second small aggregate beside the meter, which has already read the whole transcript
    # of this one conversation — so the count is the cheaper of the two by a wide margin.
    message_count = await MessageRepository(session).count_by_conversation(conversation.id)
    return ConversationDetailResponse(
        **_to_response(conversation, message_count).model_dump(),
        context=ContextWindowResponse.of(usage),
    )


async def _owned_or_404(
    session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
) -> Conversation:
    conversation = await ConversationRepository(session).get_owned(
        conversation_id, owner_id=owner_id
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return conversation


@router.post(
    "",
    response_model=ConversationDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new chat",
)
async def create_conversation(
    body: CreateConversationRequest,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> ConversationDetailResponse:
    conversation = await conversations_service.create_conversation(
        session, owner_id=user.id, title=body.title
    )
    await session.commit()
    return await _detail(session, conversation, settings)


@router.get("", response_model=list[ConversationResponse], summary="List the caller's chats")
async def list_conversations(user: CurrentUser, session: DbSession) -> list[ConversationResponse]:
    """FR-SBR-03 sidebar order — most recently updated first.

    A bare array, matching `GET /documents` (R-40(5)); no paging, because the sidebar renders
    the whole list and a user's chat count is bounded by how many they have made.

    Each row carries `message_count` (T-407) from a correlated `COUNT(*)`, so FR-SBR-03's
    "· N messages" is true on rows the client has never opened. See the field's docstring for
    why this does not reopen the R-51(4) argument that keeps `context` off this route.
    """
    counted = await ConversationRepository(session).list_by_owner_with_counts(user.id)
    return [_to_response(conversation, count) for conversation, count in counted]


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetailResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."}},
    summary="Get one chat",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> ConversationDetailResponse:
    conversation = await _owned_or_404(session, conversation_id, user.id)
    return await _detail(session, conversation, settings)


@router.patch(
    "/{conversation_id}",
    response_model=ConversationDetailResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."}},
    summary="Rename a chat",
)
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
) -> ConversationDetailResponse:
    conversation = await _owned_or_404(session, conversation_id, user.id)
    await ConversationRepository(session).rename(conversation, body.title)
    await session.commit()
    return await _detail(session, conversation, settings)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "The checkpointer could not be reached; nothing was deleted."
        },
    },
    summary="Delete a chat",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> None:
    """FR-SBR-07 — deletes the transcript **and** the LangGraph thread (R-42(11)).

    The `503` is not a formality: `delete_conversation` purges the thread *before* it commits,
    so a checkpointer outage leaves the chat intact and retryable rather than orphaning its
    graph state where nothing will ever collect it (OI-30).
    """
    conversation = await _owned_or_404(session, conversation_id, user.id)
    try:
        await conversations_service.delete_conversation(session, conversation)
    except ThreadDeletionError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _CHECKPOINTER_UNAVAILABLE) from exc
