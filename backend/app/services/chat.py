"""One chat turn, from an accepted question to a served answer (T-402).

This is the only caller of the FR-ORC-01 graph. It owns three things the graph deliberately
does not:

* **The FR-STA-04 admission check**, which runs *before* anything is written (R-51(5)). A
  refused submission is a refusal, not an FR-ORC-05 failure — nothing was attempted.
* **The user's `messages` row**, written and committed before the run starts, because
  `generate` rehydrates history from `messages` (R-42(1)) and `list_before` stops short of
  exactly this row (R-48(7)).
* **The FR-EVL-01 evaluation job**, enqueued after the answer's row has committed (R-50(5)) —
  never inside the transaction, which is the T-202 dual-write lesson.

**What it does not own is the answer.** `finalize` persists that (R-49(3) needs the read a
superstep after the gate), so this module reads back what the graph wrote rather than
composing a second copy of it.

The event stream is progress-only until the gate passes. That is not a limitation of this
module but the whole design: NFR-PRF-02's ordering, restated by R-48(1) and closed by
R-49(3), puts the FR-CIT-06 gate between generation and the client, so there is genuinely
nothing to send before it. Emitting synthetic token deltas afterwards was considered and
rejected — the text already exists server-side, so it would be an animation, not a stream.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository
from app.rag.budget import SubmissionCheck, check_submission, conversation_usage
from app.rag.errors import copy_for
from app.services.jobs import JobQueue, JobQueueError, evaluation_idempotency_key

log = structlog.get_logger(__name__)

__all__ = [
    "FEEDBACK_RECORDED",
    "ChatEvent",
    "ContextWindowExceededError",
    "TurnResult",
    "MessageEvent",
    "StageEvent",
    "record_feedback",
    "record_question",
    "run_turn",
]

#: The name T-604 binds its sink to. Deliberately **outside** the closed `graph.turn.*`
#: vocabulary R-43(5) fixed, on the `rag.eval.*` / `rag.router.*` precedent: feedback is a user
#: action on a turn that already ended, and an event inside that set would leave a consumer
#: pairing spans that never opened.
FEEDBACK_RECORDED = "chat.feedback.recorded"


class ContextWindowExceededError(Exception):
    """FR-STA-04 — the projected total would exceed NFR-CAP-01's budget.

    Carries the check so the route can report *how far* over, which is the one number that
    makes the block actionable. Not an FR-ORC-05 `FailureClass`: R-43(6) admits a member only
    where the class changes what the user should do, and nothing failed here.
    """

    def __init__(self, check: SubmissionCheck) -> None:
        super().__init__("context window exceeded")
        self.check = check


# --- events -------------------------------------------------------------------
#
# Deliberately typed rather than raw dicts: the route serialises them into SSE frames, and a
# frame is on the wire, where R-43(5)'s "no payload text ever" binds hardest. A `StageEvent`
# that *cannot* hold text is a stronger guarantee than a convention about what to put in one.


@dataclass(frozen=True, slots=True)
class StageEvent:
    """Coarse progress. Carries a stage name and nothing else — no query, no answer, no id."""

    stage: str


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """The finished turn.

    ``message`` is the persisted row, or ``None`` for an outcome that is served but not
    stored (an FR-ORC-05 failure, or an FR-ORC-02 denial). ``text`` is what to render in that
    case; when ``message`` is present the route renders its resolved segments instead.
    """

    message: Message | None
    text: str
    outcome: str | None
    error_code: str | None


type ChatEvent = StageEvent | MessageEvent


#: Node → the stage a user is shown. Several nodes map to one stage on purpose: the GUI's
#: FR-MSG-05 affordance is a typing indicator, not a progress bar, and naming `rerank`
#: separately would leak the pipeline's shape into a client that must not depend on it.
_STAGES: dict[str, str] = {
    "govern": "preparing",
    "telemetry_start": "preparing",
    "lock": "preparing",
    "screen": "preparing",
    "route": "retrieving",
    "retrieve": "retrieving",
    "rerank": "retrieving",
    "generate": "generating",
    "gate": "verifying",
    "adapt": "retrieving",
}


@dataclass(slots=True)
class TurnResult:
    """What the run produced, for the caller's post-stream work (the evaluation enqueue)."""

    answer_message_id: uuid.UUID | None = None
    outcome: str | None = None
    error_code: str | None = None
    stages: list[str] = field(default_factory=list)


# --- admission ----------------------------------------------------------------


async def record_question(
    session: AsyncSession,
    *,
    conversation: Conversation,
    query: str,
    settings: Settings | None = None,
) -> tuple[Message, int]:
    """Admit the question and write its `messages` row. Commits. Returns `(row, turn_index)`.

    **The budget check precedes the write, and the order is the requirement** (R-51's binding
    on T-402). Writing first and checking second would leave a blocked turn's unanswered
    question in the transcript permanently inflating the very meter that blocked it — a chat
    the user can neither use nor recover without deleting it.

    The commit is equally load-bearing and is not tidiness: `generate` opens its *own* session
    to rehydrate history (R-42(1) — nodes never take the request's session), so a row still
    sitting in this transaction would be invisible to it, and the model would answer a
    question it had never been shown in context.
    """
    settings = settings or get_settings()
    repo = MessageRepository(session)

    usage = await conversation_usage(session, conversation.id, settings=settings)
    check = check_submission(usage, query, settings=settings)
    if not check.allowed:
        raise ContextWindowExceededError(check)

    turn_index = await repo.count_by_conversation(conversation.id)
    row = await repo.add(
        Message(conversation_id=conversation.id, role=MessageRole.USER, content=query)
    )
    await session.commit()
    return row, turn_index


# --- the run ------------------------------------------------------------------


async def run_turn(
    *,
    conversation: Conversation,
    owner_id: uuid.UUID,
    query: str,
    user_message_id: uuid.UUID,
    turn_index: int,
    mentioned_document_ids: Sequence[uuid.UUID] = (),
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    result: TurnResult | None = None,
) -> AsyncIterator[ChatEvent]:
    """Drive one FR-ORC-01 turn, yielding progress and then the answer.

    ``result`` is filled in as the run completes so the caller can act on the outcome after
    the stream has closed — the evaluation enqueue must happen after the answer's row is
    committed *and* after the client has been served (R-50(5)).

    The `thread_id` comes from `thread_config`, never built by hand: it *is* the conversation
    id (FR-PER-02), and every caller composing that string is a chance to drift.

    Authorization rides on `RAGContext`, which is not checkpointed (R-42(3)), and
    **`is_admin` is never set from here** — R-54 requires ownership at the route, so
    `govern`'s administrator branch is unreachable on this path by construction rather than
    by this call remembering to pass `False`.
    """
    from app.rag.graph import get_graph, thread_config
    from app.rag.state import RAGContext

    settings = settings or get_settings()
    result = result if result is not None else TurnResult()

    graph = await get_graph()
    context = RAGContext(
        owner_id=owner_id,
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        sessionmaker=sessionmaker,
    )
    config = thread_config(conversation.id, settings)

    stage: str | None = None
    async for frame in graph.astream(
        {
            "query": query,
            "user_message_id": str(user_message_id),
            "turn_index": turn_index,
            "mentioned_document_ids": [str(d) for d in mentioned_document_ids],
        },
        context=context,
        config=config,
        durability=settings.graph.durability,
        stream_mode="updates",
    ):
        for node in frame:
            next_stage = _STAGES.get(node)
            if next_stage is not None and next_stage != stage:
                stage = next_stage
                result.stages.append(stage)
                yield StageEvent(stage=stage)

    # The authoritative end state, read from the checkpointer rather than accumulated from the
    # frames above. `handle_node_error` reaches state through a `Command`, and reconstructing
    # a merge of partial updates here would be a second implementation of what the checkpointer
    # already did — one that is wrong exactly on the failure path, where it matters most.
    # `GRAPH_DURABILITY=sync` (R-42(10)) is what makes this read see the final superstep.
    snapshot = await graph.aget_state(config)
    state: dict[str, Any] = dict(snapshot.values or {})

    result.outcome = state.get("outcome")
    result.error_code = state.get("error_code")
    message_id = state.get("answer_message_id")

    message: Message | None = None
    if message_id:
        result.answer_message_id = uuid.UUID(str(message_id))
        async with sessionmaker() as session:
            message = await session.get(Message, result.answer_message_id)

    # `finalize` clears `answer` once it has been persisted, so a served-but-unstored outcome
    # is the only one that still carries text — and `copy_for` covers the case where even that
    # is missing, which is FR-ERR-04's last resort reached by construction rather than by
    # omission.
    if message is not None:
        text = message.content
    else:
        text = state.get("answer") or copy_for(result.error_code)

    yield MessageEvent(
        message=message,
        text=text,
        outcome=result.outcome,
        error_code=result.error_code,
    )


async def enqueue_evaluation(queue: JobQueue, message: Message) -> None:
    """Hand the served answer to the FR-EVL-01 judge (R-50(5)). Never raises.

    Called **after** the answer's transaction has committed and after the client has been
    served: a job enqueued inside the transaction can be picked up before the row is visible
    (the T-202 dual-write lesson), and a broker outage must not fail a turn whose answer the
    user has already read. Scores are optional by FR-EVL-01's own wording — a response *may*
    carry them — so the degraded outcome here is "no chips", which is sanctioned.

    The key hashes the answer text, which is what makes FR-MSG-08 Regenerate a genuinely new
    job while a redelivery stays a duplicate (see `evaluation_idempotency_key`).
    """
    try:
        await queue.enqueue_evaluate(
            message_id=message.id,
            idempotency_key=evaluation_idempotency_key(message.id, message.content),
        )
    except JobQueueError:
        log.warning("chat.evaluation_enqueue_failed", message_id=str(message.id), exc_info=True)


def record_feedback(
    *,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    owner_id: uuid.UUID,
    feedback: Feedback | None,
) -> None:
    """OI-24's export half — put the FR-MSG-08 thumb into the log stream (NFR-OBS-05, T-403).

    **There is no sink yet, and that is the point.** NFR-OBS-05 leaves LangSmith, Langfuse and
    OpenTelemetry optional ("may"), §10.4 lists them as an unadopted scale-out option, and
    **T-604** owns the durable per-request records; OI-24 defers the calibration loop that
    would consume them. So what ships here is a *stable name with a fixed key set*. Building an
    exporter now would front-run T-604 and create a retention obligation NFR-OBS-04 has not
    settled.

    **Ids and the enum value only** — R-43(5)'s "no payload text, ever". Like the telemetry
    helpers, this function takes no parameter that could carry the question or the answer.

    A cleared rating is emitted as an explicit `None` rather than omitted: "the user cleared
    their rating" and "the user never rated" are different facts, and OI-24's consumer is
    precisely the thing that would need to tell them apart.

    Called **after** the commit, on `conversation.deleted`'s rule — a logged event must
    correspond to durable state, never to a transaction that may still roll back.
    """
    log.info(
        FEEDBACK_RECORDED,
        message_id=str(message_id),
        conversation_id=str(conversation_id),
        owner_id=str(owner_id),
        feedback=feedback.value if feedback is not None else None,
    )
