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

import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    ChatConflictResponse,
    ContextWindowExceededDetail,
    ContextWindowExceededResponse,
    NotLatestAnswerDetail,
    error_responses,
)
from app.api.events import SseFrame, TurnOutcome, TurnStage
from app.auth.dependencies import CurrentUser, DbSession, SettingsDep, StreamSessionmaker
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.document_figure import DocumentFigure
from app.db.models.message import Message
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.messages import MessageRepository
from app.rag.citations import (
    CitationFigure,
    CitationSegment,
    Segment,
    TextSegment,
    envelope_segments,
)
from app.rag.errors import (
    CONTEXT_WINDOW_EXCEEDED,
    CONTEXT_WINDOW_EXCEEDED_CODE,
    NOT_LATEST_ANSWER,
    NOT_LATEST_ANSWER_CODE,
)
from app.security.rate_limit import chat_limit, limiter, principal_or_ip_key
from app.services import chat as chat_service
from app.services.chat import ContextWindowExceededError, MessageEvent, StageEvent, TurnResult
from app.services.jobs import JobQueueDep

__all__ = ["ChatStreamFrame", "MessageResponse", "message_router", "router"]

router = APIRouter(prefix="/conversations", tags=["messages"])

#: FR-MSG-08 spells a **flat** `POST /messages/{id}/feedback`, which the chat router's
#: `/conversations` prefix cannot produce — the action bar has only the message in hand, not
#: the conversation it belongs to. Same tag, so the two stay one client namespace for T-405.
message_router = APIRouter(prefix="/messages", tags=["messages"])

_NOT_FOUND = "Conversation not found."

#: One string for all three ways a message can fail to be rateable — see `set_feedback`.
_MESSAGE_NOT_FOUND = "Message not found."

_QUERY_MAX = 8000  # TBD(§8.4) — a hard ceiling well above the NFR-CAP-01 budget FR-STA-04 enforces

#: Send and regenerate share **one** NFR-SEC-07 bucket (T-404). slowapi scopes a limit to the
#: endpoint by default (`limit_scope = lim.scope or endpoint`), which would hand one user two
#: chat budgets — and the `RATELIMIT_CHAT` threshold is a spend control over full turns, which a
#: regenerate is: same models, same ~5–6 s, same cost. `shared_limit` names the scope instead.
_CHAT_BUCKET = "chat"


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


class EvaluationResponse(BaseModel):
    """The FR-EVL-02 chips — **exactly two** metrics (R-50(1)).

    The other two FR-EVL-01 metrics are reference-based and cannot run on a live turn, which is
    why they belong to the offline harness (T-312) and their FR-ANL-04 cells read `—`
    permanently.

    Either field may be `None`: the evaluation path **fails open** (R-50(3)) and metrics are
    guarded independently, so a partial result is written rather than discarded. The whole
    object is `None` until the FR-EVL-01 job lands and stays `None` for ever if the judge never
    answers — a correct end state, not an error.

    `groundedness` is deliberately absent: the T-308 gate's structural number is not this chip
    (R-49(1), OI-34 as R-50(6) closed it), and a human thumb is not a judge score either
    (R-55(5)).
    """

    relevancy: float | None = None
    faithfulness: float | None = None


def _evaluation(raw: object) -> EvaluationResponse | None:
    """Read `messages.evaluation` back out, tolerating anything that is not one.

    `envelope_segments`' reasoning at a second JSONB column: a row written by an older or newer
    build must still render, and losing the chips is a far better failure than refusing to
    display the user's own transcript. An unreadable payload is indistinguishable from "never
    scored", which the GUI already handles because the evaluation path fails open.
    """
    if raw is None:
        return None
    try:
        return EvaluationResponse.model_validate(raw)
    except ValidationError:
        return None


class MessageResponse(BaseModel):
    """One message, in the FR-MSG-06 shape.

    `segs` is **derived, never stored as such** (R-48(4)): `messages.content` holds the answer
    with its `[S<n>]` markers intact and `messages.citations` the resolved segments. The raw
    content is deliberately not exposed — a client rendering it would show the markers, which
    is precisely what the segmentation exists to prevent.

    Both JSONB-backed fields are typed as of T-405 — `list[dict[str, Any]]` reached a generated
    client as `Record<string, never>[]`, so the FR-CIT-01 chip and FR-CIT-03 hover card would
    have been hand-written types. Neither typing can fail a read: both go through a tolerant
    coercion (`envelope_segments`, :func:`_evaluation`) that falls back rather than raising.
    """

    id: uuid.UUID
    role: MessageRole
    segs: list[Segment]
    evaluation: EvaluationResponse | None = None
    feedback: Feedback | None = None
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    created_at: datetime


#: Cited chunk -> the figures on the page it cites. Empty for every caller that has nothing to
#: resolve, which is the ordinary case and costs no query.
type FigureIndex = dict[uuid.UUID, list[DocumentFigure]]

_NO_FIGURES: FigureIndex = {}


def _cited_pages(segments: Sequence[Segment]) -> dict[uuid.UUID, int]:
    """`{chunk_id: page}` for every citation whose locator names a page (FR-CIT-07).

    Only `page` locators, and this is the whole of R-94(3)'s "selected by locator": a `section`
    or `rows` locator carries no page, so a DOCX, Markdown or CSV citation resolves nothing.
    Reading `section_index` as a page number is the failure this guard exists to prevent — it
    is an ordinal in a different space that would join happily and render another document's
    picture under someone's answer.

    A `chunkId` this build cannot parse is skipped rather than raised on, matching
    `envelope_segments`' rule that a legacy row degrades instead of costing the transcript.
    """
    pages: dict[uuid.UUID, int] = {}
    for segment in segments:
        if not isinstance(segment, CitationSegment):
            continue
        locator = segment.locator
        if locator is None or locator.kind != "page" or locator.page is None:
            continue
        try:
            pages[uuid.UUID(segment.chunkId)] = locator.page
        except ValueError:
            continue
    return pages


async def _resolve_figures(
    session: AsyncSession, segment_lists: Iterable[Sequence[Segment]], *, owner_id: uuid.UUID
) -> FigureIndex:
    """The FR-CIT-07 lookup for a whole response, in one query (R-94(3)).

    Takes every message's segments at once so a transcript costs **one** query rather than one
    per message — asserted by `test_a_transcript_issues_one_figure_query`, because "batched" is
    an intention until something counts the statements.

    Returns an empty index without touching the database when no citation names a page, which
    is free for a corpus of DOCX, Markdown or CSV, and for any deployment that never enabled
    extraction.
    """
    pages: dict[uuid.UUID, int] = {}
    for segments in segment_lists:
        pages.update(_cited_pages(segments))
    if not pages:
        return _NO_FIGURES
    return await DocumentFigureRepository(session).list_for_citations(pages, owner_id=owner_id)


def _figures(segment: CitationSegment, figures: FigureIndex) -> list[CitationFigure]:
    """The published shape of one citation's figures, or `[]`."""
    try:
        chunk_id = uuid.UUID(segment.chunkId)
    except ValueError:
        return []
    return [
        CitationFigure(
            documentId=str(figure.document_id),
            contentSha256=figure.content_sha256,
            # `''` is the column's "no caption" (NOT NULL with a default); the wire says `None`
            # so FR-CIT-07's "its caption where it has one" is one check on both sides.
            caption=figure.caption or None,
            widthPx=figure.width_px,
            heightPx=figure.height_px,
        )
        for figure in figures.get(chunk_id, ())
    ]


def _with_figures(segments: Sequence[Segment], figures: FigureIndex) -> list[Segment]:
    """Attach FR-CIT-07 figures to the citation segments that have any.

    `model_copy` rather than mutation: the segments come out of `envelope_segments`, and a
    citation with no figures must be returned **unchanged**, so that FR-CIT-07's "renders
    exactly as it does today" is a property of the object and not of a serializer that happens
    to drop an empty list.
    """
    if not figures:
        return list(segments)
    out: list[Segment] = []
    for segment in segments:
        if isinstance(segment, CitationSegment):
            resolved = _figures(segment, figures)
            if resolved:
                out.append(segment.model_copy(update={"figures": resolved}))
                continue
        out.append(segment)
    return out


def _to_response(
    message: Message, segs: Sequence[Segment], figures: FigureIndex
) -> MessageResponse:
    """One message, with its FR-CIT-07 figures already resolved.

    Both trailing parameters are **required**, and that is the point. Four route surfaces reach
    this shape through three call sites (the transcript, the SSE `message` frame on send *and*
    regenerate, and the FR-MSG-08 feedback response), and a figure that appeared on one of them
    and not another would be a defect nobody sees until a user rates an answer and watches its
    pictures vanish. A required parameter makes a fifth surface say what it wants; an optional
    one lets it forget.
    """
    return MessageResponse(
        id=message.id,
        role=message.role,
        segs=_with_figures(segs, figures),
        evaluation=_evaluation(message.evaluation),
        feedback=message.feedback,
        model_name=message.model_name,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        latency_ms=message.latency_ms,
        created_at=message.created_at,
    )


async def _responses(
    session: AsyncSession, messages: Sequence[Message], *, owner_id: uuid.UUID
) -> list[MessageResponse]:
    """A transcript, with FR-CIT-07 resolved once for the whole of it.

    Two phases on purpose: read every message's segments first, then resolve every citation's
    figures in one query. Resolving inside the loop would be N queries for an N-message chat,
    and it is the shape a later refactor drifts back into, so a statement counter pins it.
    """
    segs = [envelope_segments(m.citations, content=m.content) for m in messages]
    figures = await _resolve_figures(session, segs, owner_id=owner_id)
    return [
        _to_response(message, seg, figures) for message, seg in zip(messages, segs, strict=True)
    ]


async def _response(
    session: AsyncSession, message: Message, *, owner_id: uuid.UUID
) -> MessageResponse:
    """One message, through the same resolution as a transcript."""
    return (await _responses(session, [message], owner_id=owner_id))[0]


# --- the chat stream's frames (T-405, R-57) -------------------------------------------


class StageData(BaseModel):
    """R-43(5) made structural: this payload **cannot** carry text.

    One field, so the "progress frames carry no content" rule of R-54(2) is a property of the
    type rather than a discipline at the yield site.
    """

    stage: TurnStage


class DegradedMessage(BaseModel):
    """The served-but-unstored branch: an FR-ORC-05 failure or an FR-ORC-02 denial.

    R-54(3) keeps those turns out of `messages`, so there is no row and nothing to rate or
    regenerate — the intended consequence of not persisting a failed turn.

    **`id` is the one place a regenerate diverges from a send** (T-404), and only here: a send
    has no row so it is `null`, while a regenerate carries the *target's* id, because the client
    already has that bubble on screen and a null id would leave it unable to say which answer
    the error belongs to. `segs` is always exactly one text run.
    """

    id: uuid.UUID | None
    role: Literal["ai"] = "ai"
    segs: list[TextSegment]


class MessageFrameData(BaseModel):
    """The completed turn: one verified answer, or the copy that replaces it."""

    outcome: TurnOutcome | None = None
    error_code: str | None = Field(
        default=None,
        description=(
            "An FR-ORC-05 `FailureClass`. Absent on `abstained` and injection-`blocked` turns, "
            "which are decisions rather than failures and carry their own copy (R-44(5))."
        ),
    )
    message: MessageResponse | DegradedMessage


class DoneData(BaseModel):
    """Closes the turn. Carries the outcome again so a client that dropped the `message`
    frame still learns how it ended."""

    outcome: TurnOutcome | None = None


class ChatStageFrame(SseFrame):
    """Coarse progress. Emitted only when the stage *changes*."""

    event: Literal["stage"] = "stage"
    data: StageData


class ChatMessageFrame(SseFrame):
    """The whole verified answer, in one frame.

    There is no token streaming and that is a ruling, not an omission: R-48(1)/R-49(3) put the
    FR-CIT-06 gate between generation and the client, so nothing of the answer exists to send
    until it has passed (R-54(2)).
    """

    event: Literal["message"] = "message"
    data: MessageFrameData


class ChatDoneFrame(SseFrame):
    """End of turn."""

    event: Literal["done"] = "done"
    data: DoneData


#: What `POST /conversations/{id}/messages` and `POST /messages/{id}/regenerate` stream.
#: Published as a named component, so T-505 imports a discriminated union rather than writing
#: one. Regenerate is identical in shape to a send — that is what R-56 means by "the same shape".
type ChatStreamFrame = Annotated[
    ChatStageFrame | ChatMessageFrame | ChatDoneFrame, Field(discriminator="event")
]


def _budget_conflict(exc: ContextWindowExceededError) -> HTTPException:
    """FR-STA-04's refusal, shared by send and regenerate (R-51(5), T-404).

    A refusal, not an FR-ORC-05 failure: nothing was attempted. `409` on the R-24 processing
    lock's precedent — a state the caller resolves by acting, and R-67(1) fixes the action:
    **New chat is the only escape**, the conversation being frozen rather than broken. The
    **code** is stable and is what a client branches on; the copy is FR-STA-04's (adopted by
    R-67(2)), so T-505/T-506's composer derives from the requirement, not from this response —
    it blocks before a request exists and never sees this `message`.

    `used_tokens` is the conversation's **real** usage on both paths, never the adjusted
    projection a regenerate checks against — it feeds the FR-ANL-03 card, and a card showing a
    number the meter never displays would read as a bug in the meter.

    The detail is built as :class:`ContextWindowExceededDetail` rather than as a dict literal
    (T-405): the model is what the OpenAPI document publishes, so constructing it here is what
    makes the declared shape and the wire shape one fact instead of two that agree today.
    """
    detail = ContextWindowExceededDetail(
        error_code=CONTEXT_WINDOW_EXCEEDED_CODE,
        message=CONTEXT_WINDOW_EXCEEDED,
        used_tokens=exc.check.usage.used_tokens,
        limit_tokens=exc.check.usage.limit_tokens,
        overflow_tokens=exc.check.overflow_tokens,
    )
    return HTTPException(status.HTTP_409_CONFLICT, detail.model_dump(mode="json"))


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


# --- admission (T-405) ----------------------------------------------------------------
#
# Both chat routes are now SSE **generators** (`fastapi.sse`), and FastAPI creates a generator
# by calling it — which runs no statement in its body. So anything left inside the handler would
# execute *after* the status line is on the wire, where no HTTP status can carry a refusal. That
# is exactly the invariant both docstrings already stated ("everything that can refuse the turn
# happens before the response starts"); it just has to be expressed as a dependency now, because
# dependencies are resolved before the generator exists.
#
# The rate limit moves for a sharper reason still — see `_chat_rate_limit`.


@limiter.shared_limit(chat_limit, scope=_CHAT_BUCKET, key_func=principal_or_ip_key)
async def _chat_rate_limit(request: Request, response: Response) -> None:
    """NFR-SEC-07 for both chat routes, as a dependency rather than a route decorator.

    **Not a style choice.** slowapi's decorator wraps the endpoint in an ordinary coroutine
    function, so `inspect.isasyncgenfunction` becomes `False` — and FastAPI decides a route is a
    stream with `is_generator and issubclass(response_class, EventSourceResponse)`. Decorating
    the handler would therefore make both routes silently stop being SSE streams, with every
    test still green. `test_the_chat_routes_are_sse_streams` is the guard.

    Applied here it is also a better expression of T-404's rule: send and regenerate share **one**
    per-user bucket, and now they share it by sharing the dependency rather than by two
    decorators agreeing on a scope string. The `request`/`response` parameters are slowapi's —
    it reads the caller off the first and writes `X-RateLimit-*` onto the second, which FastAPI
    propagates to the streaming response.
    """
    return None


ChatRateLimit = Annotated[None, Depends(_chat_rate_limit)]


@dataclass(frozen=True, slots=True)
class SendAdmission:
    """What survives the checks a send must pass before its first byte."""

    conversation: Conversation
    question: Message
    turn_index: int


async def admit_send(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    _rate_limit: ChatRateLimit,
) -> SendAdmission:
    """Ownership (R-54), then the FR-STA-04 budget, and only then the question becomes a row.

    The order is load-bearing and unchanged from T-402: R-51's binding is that the budget check
    precedes the user's `messages` row, or a blocked turn leaves an unanswered question
    permanently inflating the meter that blocked it.
    """
    conversation = await _owned_or_404(session, conversation_id, user.id)
    try:
        question, turn_index = await chat_service.record_question(
            session, conversation=conversation, query=body.query, settings=settings
        )
    except ContextWindowExceededError as exc:
        raise _budget_conflict(exc) from exc
    return SendAdmission(conversation=conversation, question=question, turn_index=turn_index)


@dataclass(frozen=True, slots=True)
class RegenerationAdmission:
    """What survives the checks a regenerate must pass before its first byte."""

    conversation: Conversation
    answer: Message
    target: chat_service.RegenerationTarget
    #: Passed to the evaluation key so a *byte-identical* regeneration is still a new job. The
    #: content hash alone would make it a duplicate, and since the UPDATE cleared `evaluation`
    #: the message would stay unscored for ever (T-404; see `evaluation_idempotency_key`).
    nonce: str


async def admit_regeneration(
    message_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    _rate_limit: ChatRateLimit,
) -> RegenerationAdmission:
    """R-56's two refusals, in order: the single `404`, then `NOT_LATEST_ANSWER`/the budget."""
    repository = MessageRepository(session)
    answer = await repository.get_owned(message_id, owner_id=user.id)
    if answer is None or answer.role is not MessageRole.AI:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _MESSAGE_NOT_FOUND)

    try:
        target = await chat_service.resolve_regeneration(
            session,
            conversation_id=answer.conversation_id,
            answer=answer,
            settings=settings,
        )
    except chat_service.NotLatestAnswerError as exc:
        detail = NotLatestAnswerDetail(error_code=NOT_LATEST_ANSWER_CODE, message=NOT_LATEST_ANSWER)
        raise HTTPException(status.HTTP_409_CONFLICT, detail.model_dump(mode="json")) from exc
    except ContextWindowExceededError as exc:
        raise _budget_conflict(exc) from exc

    conversation = await _owned_or_404(session, answer.conversation_id, user.id)
    return RegenerationAdmission(
        conversation=conversation,
        answer=answer,
        target=target,
        nonce=uuid.uuid4().hex[:8],
    )


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
    return await _responses(session, messages, owner_id=user.id)


@router.post(
    "/{conversation_id}/messages",
    response_class=EventSourceResponse,
    responses={
        status.HTTP_200_OK: {
            # The union is declared rather than left to FastAPI's own stream typing: in 0.139.2
            # `RouteContext` forwards `is_sse_stream` but **not** `stream_item_field`, so a route
            # reached through `include_router` — which is every route here — emits an untyped
            # `itemSchema`. `app/openapi.py` folds this schema back into that `itemSchema`.
            "model": ChatStreamFrame,
            "description": (
                "An SSE stream. `stage` reports coarse progress and carries no content, "
                "`message` the completed and verified answer, `done` closes the turn."
            ),
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such chat for this caller."},
        status.HTTP_409_CONFLICT: {
            "model": ContextWindowExceededResponse,
            "description": "FR-STA-04 — the conversation's token budget is exhausted.",
        },
        **error_responses((429, "NFR-SEC-07 — shares the regenerate route's chat budget.")),
    },
    summary="Ask a question (SSE)",
)
async def send_message(
    admission: Annotated[SendAdmission, Depends(admit_send)],
    body: SendMessageRequest,
    user: CurrentUser,
    sessionmaker: StreamSessionmaker,
    queue: JobQueueDep,
    settings: SettingsDep,
) -> AsyncIterator[ChatStreamFrame]:
    """Run one FR-ORC-01 turn and stream its progress.

    Everything that can refuse the turn happens **before** the response starts: once a `200`
    and the first SSE frame are on the wire, a failure can only be reported inside the stream,
    where no HTTP status can carry it. So ownership (R-54), the FR-STA-04 admission check and
    the rate limit all live in `admit_send` — this body runs only once the turn is admitted,
    because FastAPI creates a generator without executing a statement of it (T-405).

    The annotation declares the **schema**; what is yielded is `frame.to_event()`, a
    `ServerSentEvent`, so the frames keep their named `event:` line — R-41 and T-508 are written
    against those names. FastAPI supports exactly this (it skips `stream_item_field` validation
    for a `ServerSentEvent` and still takes the published union from the annotation), and the
    cost — that the union is not enforced at runtime — is covered by
    `test_every_chat_frame_builder_returns_a_declared_frame`.
    """
    result = TurnResult()
    served: Message | None = None

    async for event in chat_service.run_turn(
        conversation=admission.conversation,
        owner_id=user.id,
        query=body.query,
        user_message_id=admission.question.id,
        turn_index=admission.turn_index,
        mentioned_document_ids=body.document_ids,
        sessionmaker=sessionmaker,
        settings=settings,
        result=result,
    ):
        match event:
            case StageEvent(stage=stage):
                yield ChatStageFrame(data=StageData(stage=stage)).to_event()
            case MessageEvent():
                served = event.message
                yield ChatMessageFrame(
                    data=await _message_data(event, sessionmaker=sessionmaker, owner_id=user.id)
                ).to_event()

    if served is not None and result.outcome == "answered":
        # After the row committed (`finalize` did that) and after the user was served.
        # Before `done` rather than after, so a client that disconnects the moment it has
        # its answer does not cost the message its scores.
        await chat_service.enqueue_evaluation(queue, served)

    yield ChatDoneFrame(data=DoneData(outcome=result.outcome)).to_event()


async def _message_data(
    event: MessageEvent,
    *,
    sessionmaker: StreamSessionmaker,
    owner_id: uuid.UUID,
    fallback_id: uuid.UUID | None = None,
) -> MessageFrameData:
    """The `message` frame.

    An outcome that was served but not stored — an FR-ORC-05 failure, or an FR-ORC-02 denial —
    has no row and so no `id`. The client still renders the text and may branch on
    `error_code`; there is nothing to give feedback on and nothing to regenerate, which is the
    intended consequence of not persisting a failed turn.

    **`fallback_id` is the one place a regenerate diverges from a send** (T-404), and only on
    that failure branch. The client already has a bubble on screen, so a null id there would
    leave it unable to say which answer the error belongs to; carrying the target's id lets it
    re-render the **surviving** answer non-destructively. The event *sequence* is identical,
    which is what "the same shape as a send" means.

    **Async, and it opens its own session, because of FR-CIT-07.** Neither streaming handler
    carries a `DbSession` — deliberately, T-210: a stream that held a request-scoped session
    would pin a pool slot for the length of a turn. It takes the handler's sessionmaker and
    opens a short one for the figure lookup, which is `_persist_turn`'s own pattern. The
    alternative — leaving this frame without figures — is the split R-94 would be discovered
    by: an answer with no pictures until the user reloads.

    The degraded branch opens nothing. `DegradedMessage.segs` is `list[TextSegment]`, so there
    is no citation to resolve and no query to make.
    """
    message: MessageResponse | DegradedMessage
    if event.message is not None:
        async with sessionmaker() as session:
            message = await _response(session, event.message, owner_id=owner_id)
    else:
        message = DegradedMessage(id=fallback_id, segs=[TextSegment(text=event.text)])
    return MessageFrameData(outcome=event.outcome, error_code=event.error_code, message=message)


@message_router.post(
    "/{message_id}/regenerate",
    response_class=EventSourceResponse,
    responses={
        status.HTTP_200_OK: {
            "model": ChatStreamFrame,  # see `send_message` for why this is declared
            "description": (
                "An SSE stream, identical in shape to a send: `stage` frames carrying no "
                "content, one `message` frame with the replacement, then `done`. The `message` "
                "frame always carries the target's id — on failure it carries the *unchanged* "
                "answer plus `error_code`."
            ),
        },
        status.HTTP_404_NOT_FOUND: {"description": "No AI message with this id for this caller."},
        status.HTTP_409_CONFLICT: {
            "model": ChatConflictResponse,
            "description": (
                "`NOT_LATEST_ANSWER` — a later turn has landed; or "
                "`CONTEXT_WINDOW_EXCEEDED` — FR-STA-04's budget. Branch on `error_code`."
            ),
        },
        **error_responses((429, "NFR-SEC-07 — shares the send route's per-user chat budget.")),
    },
    summary="Regenerate an answer (SSE)",
)
async def regenerate_message(
    admission: Annotated[RegenerationAdmission, Depends(admit_regeneration)],
    user: CurrentUser,
    sessionmaker: StreamSessionmaker,
    queue: JobQueueDep,
    settings: SettingsDep,
) -> AsyncIterator[ChatStreamFrame]:
    """FR-MSG-08's Regenerate — re-run the question and **replace** the answer (T-404, R-56).

    Addressed by message id like its sibling `feedback` route, and refused the same way: absent,
    foreign or non-AI targets are one `404` with one copy (R-55(2)), for an administrator too.
    The second refusal is this route's own — **only the latest AI answer may be regenerated**
    (`409` + `NOT_LATEST_ANSWER`), because rewriting a mid-transcript answer silently invalidates
    every turn generated from it and the spec has no cascade rule.

    **The row is replaced, never appended.** §4.16 makes the transcript what the user sees and
    the FR-ANL cards count, and R-51(4) derives the NFR-CAP-01 budget from it — so an appended
    answer would show one question answered twice and charge the conversation for both.
    `finalize` does the write, through `RAGState.regenerate_message_id`, which is what keeps it
    idempotent across a resume.

    **A failed re-run leaves the answer exactly as it was.** `_should_persist` excludes `error`
    (R-54(3)), so the UPDATE is simply never reached — and because `evaluation`/`feedback` clear
    inside that same UPDATE rather than here, a failure cannot destroy the scores or the rating
    of an answer that still exists. An **abstained or injection-blocked** re-run does replace, on
    FR-MSG-08's unconditional wording and R-23's "an abstention is a response": the common cause
    is that the user lost access to the document the old answer cited, and keeping that prose
    would preserve text whose sources are gone. There is no undo, which is a cost the ruling
    records rather than hides.

    **Not gated by the R-24 lock at this route** — R-55(1) honoured, not contradicted. Regenerate
    *takes* the gate (the graph's `lock` node acquires it exactly as for a send), which is what
    pauses the caller's document affordances; it does not *check* it. A route-level `409` would
    refuse a regenerate in one chat because a different chat of the same user is mid-turn, since
    R-43(1) keys the gate on the caller — the precise defect R-55(1) rejected for feedback — and
    R-43(2) names "Regenerate over a live turn" as the case its token-matched release exists to
    make compose. Two concurrent regenerates of one row are last-write-wins and benign: there is
    no read-modify-write, so either order commits a complete, internally consistent answer.

    Its refusals — the single `404` and the two `409`s — live in `admit_regeneration`, because
    a generator handler's body runs after the `200` (see `send_message`, T-405).
    """
    target = admission.target
    answer = admission.answer
    result = TurnResult()
    served: Message | None = None

    async for event in chat_service.run_turn(
        conversation=admission.conversation,
        owner_id=user.id,
        query=target.question.content,
        user_message_id=target.question.id,
        turn_index=target.turn_index,
        regenerate_message_id=answer.id,
        sessionmaker=sessionmaker,
        settings=settings,
        result=result,
    ):
        match event:
            case StageEvent(stage=stage):
                yield ChatStageFrame(data=StageData(stage=stage)).to_event()
            case MessageEvent():
                served = event.message
                yield ChatMessageFrame(
                    data=await _message_data(
                        event,
                        sessionmaker=sessionmaker,
                        owner_id=user.id,
                        fallback_id=answer.id,
                    )
                ).to_event()

    if served is not None:
        chat_service.record_regeneration(
            message_id=served.id,
            conversation_id=served.conversation_id,
            owner_id=user.id,
            turn_index=target.turn_index,
            outcome=result.outcome,
        )
        if result.outcome == "answered":
            await chat_service.enqueue_evaluation(queue, served, nonce=admission.nonce)

    yield ChatDoneFrame(data=DoneData(outcome=result.outcome)).to_event()


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
    generating" is discharged as the GUI affordance R-71(1) governs (OI-31, now resolved): the
    client-side in-flight signal disables the control, and the `409` this route deliberately does
    not raise is one the client would then have to reconcile for nothing.

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
    return await _response(session, message, owner_id=user.id)
