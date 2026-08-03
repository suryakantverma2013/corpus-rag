"""``/conversations/{id}/messages`` — the chat surface (T-402, FR-CST-03, §4.16).

Two routes: send a question (SSE) and read the transcript, plus FR-MSG-08's flat
``POST /messages/{id}/feedback`` on a second router (T-403). All three are **owner-only** — R-54,
which closes OI-33 by making an administrator's turn on someone else's conversation
unreachable rather than by ruling on what it would retrieve. The sharper half of that issue is
that `finalize` persists into the conversation, so an admin's foreign turn would write a
message the owner never asked for into a transcript FR-MSG-06/§4.16 make durable, exportable
and counted by the FR-ANL cards.

**The send route streams, and what it streams is progress.** NFR-PRF-02 requires production
responses to stream; R-48(1) and R-49(3) put the FR-CIT-06 gate between generation and the
client, so nothing of the answer exists to send until it has passed. The stream therefore
carries `stage` frames with no content, then one `message` frame with the whole verified
answer, then `done` — which is what FR-MSG-05's typing indicator needs and no more. There is
deliberately no `stream: false` variant: the Source A sketch carried that flag, NFR-PRF-02
fixes the behaviour, and a second non-streaming shape would be a second thing to keep correct.

Field naming has one deliberate seam. The route's own fields are snake_case like every other
router here; the **segment** keys are camelCase (`isCite`, `chunkId`) because that shape is
fixed by FR-MSG-06 and is the persisted `messages.citations` contract `workers/evaluate.py`
reads. Renaming them at this boundary would put a translation between the gate's view of a
citation and the user's.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth.dependencies import CurrentUser, DbSession, SettingsDep, StreamSessionmaker
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.messages import MessageRepository
from app.rag.citations import envelope_segments
from app.rag.errors import CONTEXT_WINDOW_EXCEEDED, CONTEXT_WINDOW_EXCEEDED_CODE
from app.security.rate_limit import chat_limit, limiter, principal_or_ip_key
from app.services import chat as chat_service
from app.services.chat import ContextWindowExceededError, MessageEvent, StageEvent, TurnResult
from app.services.jobs import JobQueueDep

__all__ = ["MessageResponse", "message_router", "router"]

router = APIRouter(prefix="/conversations", tags=["messages"])

#: FR-MSG-08 spells a **flat** `POST /messages/{id}/feedback`, which the chat router's
#: `/conversations` prefix cannot produce — the action bar has only the message in hand, not
#: the conversation it belongs to. Same tag, so the two stay one client namespace for T-405.
message_router = APIRouter(prefix="/messages", tags=["messages"])

_NOT_FOUND = "Conversation not found."

#: One string for all three ways a message can fail to be rateable — see `set_feedback`.
_MESSAGE_NOT_FOUND = "Message not found."

_QUERY_MAX = 8000  # TBD(§8.4) — a hard ceiling well above the NFR-CAP-01 budget FR-STA-04 enforces


class SendMessageRequest(BaseModel):
    """FR-CMP-01's composer payload.

    `document_ids` are the FR-CMP-04 `@`-mentions and they **narrow** the retrieval scope
    (R-46(1)) — they are AND-ed with the caller's ambient scope, never unioned, so a mention
    naming a document outside it retrieves nothing rather than reaching it.
    """

    query: str = Field(min_length=1, max_length=_QUERY_MAX)
    document_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def _stripped(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("query must not be blank")
        return query


class FeedbackRequest(BaseModel):
    """FR-MSG-08's thumbs payload — three states, and the key is **required**.

    FR-MSG-06 types the field `'up'|'down'|null`, so clearing is reachable (FR-MSG-08 says the
    control *toggles*, and a second click on the lit thumb turns it off) and has to be
    expressible. `Feedback` has no `NONE` member and must not grow one: `NULL` is already the
    column's absent state, and a third enum value would make "cleared" and "never rated" two
    representations of one fact.

    Required rather than defaulted to `None`, which is `RenameConversationRequest.title`'s
    shape: with a default, `{}` would silently erase a rating the user gave. It is a `422`.
    """

    feedback: Feedback | None


class MessageResponse(BaseModel):
    """One message, in the FR-MSG-06 shape.

    `segs` is **derived, never stored as such** (R-48(4)): `messages.content` holds the answer
    with its `[S<n>]` markers intact and `messages.citations` the resolved segments. The raw
    content is deliberately not exposed — a client rendering it would show the markers, which
    is precisely what the segmentation exists to prevent.

    `evaluation` carries **two** keys at most, `{relevancy, faithfulness}` (R-50(1)) — the
    other two FR-EVL-01 metrics are reference-based and cannot run on a live turn. It is
    `None` until the FR-EVL-01 job lands, and stays `None` for ever if the judge never
    answers, which is a correct end state (the evaluation path fails open).
    """

    id: uuid.UUID
    role: MessageRole
    segs: list[dict[str, Any]]
    evaluation: dict[str, Any] | None = None
    feedback: Feedback | None = None
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    created_at: datetime


def _to_response(message: Message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        role=message.role,
        segs=envelope_segments(message.citations, content=message.content),
        evaluation=message.evaluation,
        feedback=message.feedback,
        model_name=message.model_name,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        latency_ms=message.latency_ms,
        created_at=message.created_at,
    )


async def _owned_or_404(
    session: AsyncSession, conversation_id: uuid.UUID, owner_id: uuid.UUID
) -> Conversation:
    """R-54. No administrator widening, and a foreign id is `404`, never `403` (NFR-SEC-02)."""
    conversation = await ConversationRepository(session).get_owned(
        conversation_id, owner_id=owner_id
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return conversation


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageResponse],
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."}},
    summary="List a chat's messages",
)
async def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> list[MessageResponse]:
    """The transcript, oldest first.

    Ordered by `messages.seq`, never `created_at` (T-108): a turn writes the question and the
    answer close together, and `created_at` is the *transaction* timestamp, so any tiebreak on
    the random UUID `id` is a coin flip on rendering the answer above the question.

    R-45(6)'s binding — read history through `app.rag.history` rather than mapping rows by
    hand — governs the **prompt** path, where `MessageRole.AI` is `"ai"` and `compose_messages`
    silently drops a role it does not know. It is discharged inside `generate`. This is the
    API's own view and maps the enum explicitly, which is the same trap named once more:
    the wire value is `"ai"`, not `"assistant"`.
    """
    await _owned_or_404(session, conversation_id, user.id)
    messages = await MessageRepository(session).list_by_conversation(conversation_id)
    return [_to_response(message) for message in messages]


@router.post(
    "/{conversation_id}/messages",
    response_class=EventSourceResponse,
    responses={
        status.HTTP_200_OK: {
            "description": (
                "An SSE stream. `stage` reports coarse progress and carries no content, "
                "`message` the completed and verified answer, `done` closes the turn."
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."},
        status.HTTP_409_CONFLICT: {
            "description": "FR-STA-04 — the conversation's token budget is exhausted."
        },
    },
    summary="Ask a question (SSE)",
)
@limiter.limit(chat_limit, key_func=principal_or_ip_key)
async def send_message(
    request: Request,
    # Unused by the handler, but slowapi writes its `X-RateLimit-*` headers onto it and
    # raises if the endpoint does not declare one.
    response: Response,
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    user: CurrentUser,
    session: DbSession,
    sessionmaker: StreamSessionmaker,
    queue: JobQueueDep,
    settings: SettingsDep,
) -> EventSourceResponse:
    """Run one FR-ORC-01 turn and stream its progress.

    Everything that can refuse the turn happens **before** the response starts: once a `200`
    and the first SSE frame are on the wire, a failure can only be reported inside the stream,
    where no HTTP status can carry it. So ownership (R-54) and the FR-STA-04 admission check
    are both resolved here, in that order, and only then does the question become a row.
    """
    conversation = await _owned_or_404(session, conversation_id, user.id)

    try:
        question, turn_index = await chat_service.record_question(
            session, conversation=conversation, query=body.query, settings=settings
        )
    except ContextWindowExceededError as exc:
        # A refusal, not an FR-ORC-05 failure (R-51(5)): nothing was attempted. `409` on the
        # R-24 processing lock's precedent — a state the caller resolves by acting. The
        # **code** is stable and is what a client branches on; the copy is provisional while
        # OI-26(c) (the escape path) belongs to T-505.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error_code": CONTEXT_WINDOW_EXCEEDED_CODE,
                "message": CONTEXT_WINDOW_EXCEEDED,
                "used_tokens": exc.check.usage.used_tokens,
                "limit_tokens": exc.check.usage.limit_tokens,
                "overflow_tokens": exc.check.overflow_tokens,
            },
        ) from exc

    result = TurnResult()

    async def publish() -> Any:
        served: Message | None = None
        async for event in chat_service.run_turn(
            conversation=conversation,
            owner_id=user.id,
            query=body.query,
            user_message_id=question.id,
            turn_index=turn_index,
            mentioned_document_ids=body.document_ids,
            sessionmaker=sessionmaker,
            settings=settings,
            result=result,
        ):
            match event:
                case StageEvent(stage=stage):
                    yield {"event": "stage", "data": json.dumps({"stage": stage})}
                case MessageEvent():
                    served = event.message
                    yield {"event": "message", "data": json.dumps(_message_frame(event))}

        if served is not None and result.outcome == "answered":
            # After the row committed (`finalize` did that) and after the user was served.
            # Before `done` rather than after, so a client that disconnects the moment it has
            # its answer does not cost the message its scores.
            await chat_service.enqueue_evaluation(queue, served)

        yield {"event": "done", "data": json.dumps({"outcome": result.outcome})}

    return EventSourceResponse(publish(), ping=int(settings.sse.ping_seconds))


def _message_frame(event: MessageEvent) -> dict[str, Any]:
    """The `message` frame.

    An outcome that was served but not stored — an FR-ORC-05 failure, or an FR-ORC-02 denial —
    has no row and so no `id`. The client still renders the text and may branch on
    `error_code`; there is nothing to give feedback on and nothing to regenerate, which is the
    intended consequence of not persisting a failed turn.
    """
    frame: dict[str, Any] = {"outcome": event.outcome, "error_code": event.error_code}
    if event.message is not None:
        frame["message"] = _to_response(event.message).model_dump(mode="json")
    else:
        frame["message"] = {
            "id": None,
            "role": MessageRole.AI.value,
            "segs": [{"text": event.text}],
        }
    return frame


@message_router.post(
    "/{message_id}/feedback",
    response_model=MessageResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No AI message with this id for this caller."}
    },
    summary="Rate an answer",
)
async def set_feedback(
    message_id: uuid.UUID,
    body: FeedbackRequest,
    user: CurrentUser,
    session: DbSession,
) -> MessageResponse:
    """FR-MSG-08's 👍/👎, and FR-MSG-06's third state, the clear (T-403, R-55).

    Addressed by **message id**, not through its conversation, because FR-MSG-08 spells the
    path that way and the action bar has only the message. Ownership is therefore a join and
    it lives in the query (`MessageRepository.get_owned`) — R-54: a message in someone else's
    chat is `404`, never `403`, for an administrator exactly as for anyone else.

    **One status and one string cover all three ways this can fail.** A message that does not
    exist, one in a foreign chat, and one whose role is `user` are indistinguishable in the
    response on purpose: distinguishing them would make the route a probe for which ids exist
    (NFR-SEC-02). The wrong-role case is a `404` rather than the `409` this surface uses for
    `NotRetryableError` / `NotReplaceableError` — those answer a request a *correct* client
    could make against state that moved under it, whereas FR-MSG-04 puts the action bar beneath
    AI answers only, so no correct client produces this one. It is also already the answer
    R-54(3) gives an errored turn: nothing was persisted, so there is nothing to rate.

    **Not gated by the R-24 processing lock**, although FR-MSG-08 names it. R-43(4) enforces
    that gate at exactly R-24's four *file* verbs and states read routes are never gated; this
    is a write, but on a row a finished turn already committed and served. The in-flight answer
    has no row yet, so it can never be this route's target — and because the lock is keyed on
    the caller, gating would refuse feedback on one message because a *different* one is
    generating, which is not the requirement's clause but a bug. FR-MSG-08's "disabled while
    generating" is discharged as the GUI affordance OI-31 already governs, which also leaves
    OI-31's unpredicted-`409` gap unmanufactured.

    Idempotent: the same value twice is a `200`. The column is state, not an event log.

    Touches `messages.feedback` and nothing else — in particular never `evaluation`, which is
    DeepEval's alone (R-49(1), OI-34 as R-50(6) closed it). A human thumb is not a judge score,
    and the FR-EVL-02 chip must not move because someone disagreed with it.
    """
    repository = MessageRepository(session)
    message = await repository.get_owned(message_id, owner_id=user.id)
    if message is None or message.role is not MessageRole.AI:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MESSAGE_NOT_FOUND)

    await repository.set_feedback(message, body.feedback)
    await session.commit()

    chat_service.record_feedback(
        message_id=message.id,
        conversation_id=message.conversation_id,
        owner_id=user.id,
        feedback=body.feedback,
    )
    return _to_response(message)
