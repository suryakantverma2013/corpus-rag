"""FR-MSG-09 — the optional answer from the model's own training (R-98, T-727).

**Why this is not a graph node.** The fallback skips retrieve, rerank, generate-with-context
and the gate: it is a `ChatClient` call and a persist. Routing it through `run_turn` would mean
adding a `RAGState` channel whose only purpose is to make every node skip itself, so R-98's
scope line was corrected before any code was written and `RAGState` is untouched. The cost is
stated rather than hidden: this path does **not** inherit the graph's telemetry and emits its
own event, and it must repeat the R-24 lock discipline rather than inherit it.

**What makes it safe is what it cannot do**, not what it promises:

* It cites nothing *by construction* — no passages are supplied, so no ``[S<n>]`` marker can
  resolve and the `citations` envelope is empty. FR-CIT-06(2) is unviolatable here rather than
  merely unviolated.
* It is never evaluated. R-50 already skips messages citing nothing, so faithfulness is
  *undefined* rather than zero.
* It is written with ``ungrounded=True``, which `MessageRepository.list_before` and
  `list_tail` filter out — so it can never become context for a later grounded answer.

**It is only ever reachable by a deliberate act.** `UNGROUNDED_FALLBACK_ENABLED` decides
whether the GUI offers the control; the request is still one person, one question. There is no
path from an abstention to this text without someone asking for it, which is R-98(7): an
automatic fallback would make every corpus failure look like a successful answer, and three
abstentions against a calculus textbook are how B-007's corrupt text layer was found.
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository
from app.rag.citations import envelope_cites_nothing
from app.rag.history import load_history, to_messages
from app.services.llm import build_chat_client
from app.services.model_selection import resolve_models

log = structlog.get_logger(__name__)


class UngroundedDisabledError(Exception):
    """`UNGROUNDED_FALLBACK_ENABLED` is off, so the control should never have been offered."""


class NotAnAbstentionError(Exception):
    """The target answered from the documents, so there is nothing to fall back from."""


#: The instructions-only `system` message. R-44(3)'s shape still applies even with no retrieved
#: text to fence: exactly one `system` message, carrying instructions and no untrusted bytes.
#:
#: It tells the model it has **no documents** rather than leaving that implicit. Without it a
#: model primed by the conversation's earlier grounded turns tends to write as though it were
#: still citing, and the answer's honesty is the entire product claim being made here.
_SYSTEM = (
    "You are answering from your own general knowledge. You have NOT been given any "
    "documents, passages or sources for this question, and you must not cite any, invent "
    "references, or imply that your answer comes from the user's documents. If you are not "
    "confident, say so plainly. Answer directly and concisely."
)


async def answer_from_general_knowledge(
    session: AsyncSession,
    *,
    conversation: Conversation,
    target: Message,
    settings: Settings | None = None,
) -> Message:
    """Generate and persist one FR-MSG-09 answer, appended beneath ``target``.

    ``target`` is the abstention the user pressed the control on. It is **not** modified: the
    abstention stays in the transcript because it is the record that the corpus could not
    answer, and R-98(1) leans on exactly that record. The new row is appended after it.

    Raises :class:`UngroundedDisabledError` when the deployment has not enabled the control,
    and :class:`NotAnAbstentionError` when the target is a real answer — both are conditions a
    correct client cannot produce, so they are refusals rather than errors.
    """
    settings = settings or get_settings()
    if not settings.ungrounded.fallback_enabled:
        raise UngroundedDisabledError("UNGROUNDED_FALLBACK_ENABLED is false")
    # An abstention cites nothing. Checked on the stored row rather than on a flag the client
    # sends, because "this turn abstained" is a fact about the transcript, not a claim.
    #
    # **Asked of the segments, never of the column.** The `abstain` node persists a complete
    # envelope - refusal text as segments, an empty `source_ids` - so `messages.citations` is a
    # non-empty dict on every real abstention and `if target.citations:` refused all of them.
    # T-727's live pass found it; the unit tests could not, because their fixtures wrote
    # `citations=None`, a shape the pipeline never produces.
    if not envelope_cites_nothing(target.citations):
        raise NotAnAbstentionError("the target answer carries citations")
    if target.ungrounded:
        raise NotAnAbstentionError("the target is itself an ungrounded answer")

    repo = MessageRepository(session)
    # History *excluding* the abstention itself and everything after it, so the model sees the
    # conversation as it stood when the question was asked. `list_before` already drops earlier
    # ungrounded answers (R-98(5)), so this cannot compound.
    history = await load_history(session, conversation.id, until_message_id=target.id)
    question = history[-1].content if history else ""
    messages = [
        {"role": "system", "content": _SYSTEM},
        *to_messages(history[:-1]),
        {"role": "user", "content": question},
    ]

    # The same resolved slot the grounded turn uses (R-83/R-87): an operator who has pointed
    # the deployment at a different chat model must not find this path still on the old one.
    models = await resolve_models(session, settings)
    client = build_chat_client(settings)
    started = time.monotonic()
    stream = await client.stream_answer(
        messages,
        max_output_tokens=settings.llm.max_output_tokens,
        model=models.chat,
    )
    answer = await stream.collect()
    latency_ms = int((time.monotonic() - started) * 1000)

    row = await repo.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.AI,
            content=answer.text,
            # Empty by construction, not cleared afterwards: nothing was supplied to cite.
            citations=None,
            ungrounded=True,
            model_name=answer.model,
            prompt_tokens=answer.prompt_tokens,
            completion_tokens=answer.completion_tokens,
            latency_ms=latency_ms,
        )
    )
    await session.commit()

    # Its own event, outside the closed `graph.turn.*` vocabulary R-43(5) fixed — this is not a
    # graph turn and counting it as one would corrupt the turn metrics. Ids and scalars only.
    log.info(
        "chat.ungrounded.answered",
        conversation_id=str(conversation.id),
        target_message_id=str(target.id),
        answer_message_id=str(row.id),
        model_name=answer.model,
        latency_ms=latency_ms,
    )
    return row


def is_offerable(target: Message, settings: Settings | None = None) -> bool:
    """Whether the GUI should show the control on ``target``.

    One predicate, so the route and any future surface agree on what "offerable" means rather
    than each re-deriving it — the drift R-71(1) had to reconcile between a client-side signal
    and a server `409`.
    """
    settings = settings or get_settings()
    return (
        settings.ungrounded.fallback_enabled
        and target.role is MessageRole.AI
        and envelope_cites_nothing(target.citations)
        and not target.ungrounded
    )


__all__ = [
    "NotAnAbstentionError",
    "UngroundedDisabledError",
    "answer_from_general_knowledge",
    "is_offerable",
]
