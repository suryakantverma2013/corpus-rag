"""FR-EVL-01 post-hoc evaluation task — scores one served answer (T-309, R-50).

Post-hoc in the strongest sense: this runs after `finalize`, after the `messages` row is
committed, after the user has read the answer. Nothing waits on it and nothing it does can
change what was served. That single fact settles the failure policy — **evaluation fails
open**. The convention this codebase alternates on is *what the degraded output is*, not how
important the stage sounds: `gate` fails closed because its degraded output is "serve an
answer whose grounding was never checked", which is the absence of the control rather than a
degradation of it. Here the degraded output is "no chips", which FR-EVL-01 sanctions in its
own wording (a response **may** carry scores) and for which FR-ANL-04 already ships copy.

So a message whose judge never answers keeps `evaluation` NULL for ever, and that is a
correct end state, not a stuck one.

**The `messages.citations` envelope is a contract with T-402** (R-50). It carries the resolved
segments *and* `source_ids` — the ordered grounding set the answer's `[S<n>]` markers index
into. The ordered list is not recoverable from the segments alone: an answer citing `[S2]` and
`[S5]` leaves positions 1, 3 and 4 unrepresented, and without them `split_answer_segments`
cannot be replayed. R-49(a) forbids writing a second parser to work around that, so the list
is persisted instead.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from arq.worker import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository
from app.db.repositories.retrieval import fetch_chunks
from app.db.session import get_sessionmaker
from app.rag.evaluation import (
    EVAL_COMPLETED,
    EVAL_FAILED,
    EVAL_SKIPPED,
    EvaluationScores,
    build_evaluator,
    structural_coverage,
)
from app.rag.retrieval import RetrievalFilter
from app.services.llm import ChatClient, get_chat_client
from workers.common import backoff, is_final_attempt

log = structlog.get_logger(__name__)

# Skip reasons. Each is a *correct* end state, not a failure — hence `EVAL_SKIPPED` rather
# than `EVAL_FAILED`, which is reserved for a judge that could not be reached.
_SKIP_DISABLED = "disabled"
_SKIP_NOT_FOUND = "not_found"
_SKIP_NOT_AI = "not_ai_message"
_SKIP_ALREADY = "already_evaluated"
_SKIP_NO_CITATIONS = "no_citations"
_SKIP_NO_CONTEXT = "context_unavailable"
_SKIP_NO_QUESTION = "no_question"


@dataclass(slots=True)
class _Deps:
    """What the evaluation task needs. Populated by `workers.main`'s `on_startup`.

    No storage, no embedder, no scanner: this task reads two tables and calls one model.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    chat: ChatClient
    settings: Settings

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> _Deps:
        return cls(
            sessionmaker=ctx.get("sessionmaker") or get_sessionmaker(),
            chat=ctx.get("chat") or get_chat_client(),
            settings=ctx.get("settings") or get_settings(),
        )


@dataclass(frozen=True, slots=True)
class _Subject:
    """Everything the judge needs, read before any network call."""

    question: str
    answer: str
    #: The text of the **cited** chunks, and deliberately not the whole grounding set.
    #:
    #: This is what makes Faithfulness the semantic counterpart of the T-308 gate rather than
    #: a loosely related number: it asks whether the passages this answer *claims* to rest on
    #: actually support what it says — FR-CIT-06 check (4), the exact property R-49(1)
    #: deferred to this task. Feeding it all eight reranked chunks would dilute the judge with
    #: context the answer never invoked.
    context: tuple[str, ...]
    source_ids: tuple[str, ...]
    cited_ids: tuple[uuid.UUID, ...]
    conversation_id: uuid.UUID


async def evaluate_message(ctx: dict[str, Any], message_id: str) -> None:
    """Score one AI answer and write `messages.evaluation` (FR-EVL-01).

    One positional string, an ID only — the same wire contract as the two document tasks,
    minus their `job_id`, because this job has no `knowledge_jobs` row (see
    `app.services.jobs.EVALUATE_TASK_NAME`).
    """
    deps = _Deps.from_ctx(ctx)
    job_try = int(ctx.get("job_try", 1))

    if not deps.settings.eval.enabled:
        return _skip(message_id, _SKIP_DISABLED)

    # Read, then release. Nothing may hold a session across the judge's five model calls —
    # the T-205 rule: a write transaction pinned across minutes of provider latency holds the
    # cluster's `xmin` horizon and is killed by any idle-in-transaction timeout, after the
    # cost has already been paid.
    async with deps.sessionmaker() as session:
        subject, skip = await _load_subject(session, message_id)
    if skip is not None:
        return _skip(message_id, skip)
    assert subject is not None  # noqa: S101 — narrowed by `skip is None`

    started = time.perf_counter()
    scores = await build_evaluator(deps.chat, deps.settings).score(
        question=subject.question,
        answer=subject.answer,
        context=subject.context,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if scores.empty:
        # Every metric failed. Retryable through arq's own backoff while attempts remain —
        # a rate limit or a provider blip is exactly what a retry fixes. On the final attempt
        # we return rather than raise: arq checks `max_tries` at the top of `run_job`, so a
        # task can never observe its own dead-letter, and raising here would only produce a
        # noisy traceback for an outcome that is by design survivable.
        if not is_final_attempt(job_try, settings=deps.settings):
            raise Retry(defer=backoff(job_try, settings=deps.settings))
        log.warning(EVAL_FAILED, message_id=message_id, attempts=job_try, elapsed_ms=elapsed_ms)
        return None

    async with deps.sessionmaker() as session:
        repo = MessageRepository(session)
        message = await session.get(Message, uuid.UUID(message_id))
        if message is None:
            return _skip(message_id, _SKIP_NOT_FOUND)
        await repo.set_evaluation(message, scores.as_payload())
        await session.commit()

    _log_completed(message_id, subject, scores, elapsed_ms)
    return None


async def _load_subject(
    session: AsyncSession, message_id: str
) -> tuple[_Subject | None, str | None]:
    """Read the answer, its question and its cited passages. Returns `(subject, skip_reason)`."""
    message = await session.get(Message, uuid.UUID(message_id))
    if message is None:
        return None, _SKIP_NOT_FOUND
    if message.role is not MessageRole.AI:
        return None, _SKIP_NOT_AI
    if message.evaluation is not None:
        # Redelivery safety. A regenerated answer is a different job (its idempotency key
        # hashes the content) *and* T-402 must clear this column, or the re-run stops here.
        return None, _SKIP_ALREADY

    envelope = message.citations if isinstance(message.citations, dict) else {}
    source_ids = tuple(str(s) for s in envelope.get("source_ids") or ())
    cited = _cited_ids(envelope)
    if not cited:
        # An abstained or blocked turn cites nothing (the `abstain` node writes an empty
        # citation list), so this one condition excludes them without needing the graph's
        # `outcome` — which the worker has no access to anyway. It is also the honest
        # boundary: with no cited passage there is no retrieval context, and Faithfulness
        # against an empty context is not a low score, it is a meaningless one.
        return None, _SKIP_NO_CITATIONS

    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is None:
        return None, _SKIP_NOT_FOUND

    question = await _question_for(session, message)
    if not question:
        return None, _SKIP_NO_QUESTION

    hits = await fetch_chunks(
        session,
        cited,
        filters=RetrievalFilter(
            owner_id=conversation.owner_id,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
        ),
    )
    if not hits:
        # Every cited document has been deleted since the answer was served. Scoring against
        # an empty context would return 0.0 and label a good answer unfaithful, which is
        # worse than leaving it unscored.
        return None, _SKIP_NO_CONTEXT

    subject = _Subject(
        question=question,
        answer=message.content,
        context=tuple(hit.chunk_text for hit in hits),
        source_ids=source_ids,
        cited_ids=cited,
        conversation_id=message.conversation_id,
    )
    return subject, None


def _cited_ids(envelope: dict[str, Any]) -> tuple[uuid.UUID, ...]:
    """Distinct chunk ids the answer actually cited, in first-citation order."""
    seen: dict[str, None] = {}
    for segment in envelope.get("segments") or ():
        if not isinstance(segment, dict):
            continue
        chunk_id = segment.get("chunkId")
        if isinstance(chunk_id, str) and chunk_id:
            seen.setdefault(chunk_id, None)
    out: list[uuid.UUID] = []
    for value in seen:
        try:
            out.append(uuid.UUID(value))
        except ValueError:
            continue
    return tuple(out)


async def _question_for(session: AsyncSession, message: Message) -> str:
    """The user turn this answer replies to — DeepEval's `input`.

    Delegates to the repository (T-404): Regenerate needs the identical derivation to know
    *which question to re-run*, and two walks of the same `seq` adjacency would be two chances
    for the judge to score an answer against a different question than the one regenerated it.
    The read is now `LIMIT 1` on the index rather than the whole preceding transcript.
    """
    question = await MessageRepository(session).preceding_user_message(
        message.conversation_id, message_id=message.id
    )
    return question.content if question is not None else ""


def _skip(message_id: str, reason: str) -> None:
    log.info(EVAL_SKIPPED, message_id=message_id, reason=reason)
    return None


def _log_completed(
    message_id: str, subject: _Subject, scores: EvaluationScores, elapsed_ms: int
) -> None:
    """The R-49(b) correlation record — the reason this task is the named revisit instrument.

    Faithfulness and the T-308 gate's structural coverage measure the same property
    (FR-CIT-06(4)) by different means, and R-49(1) chose the structural one for the pre-serve
    gate partly on the argument that this job would supply the evidence to revisit that. The
    two numbers only constitute evidence if they are recorded *together*, so they are emitted
    in one event rather than left to be joined later from two places.

    Coverage is recomputed here rather than persisted: it lives in `RAGState`, which is the
    checkpointer's, and writing it beside the DeepEval scores is exactly the conflation
    R-49(1) forbids — `messages.evaluation` is DeepEval's alone. Recomputation is exact and
    free, because both callers run the same two pure functions over the same text.

    No query text, no answer text, no passage text — R-43(5)'s rule, which applies to every
    `rag.*` event and not only to the `graph.turn.*` set.
    """
    coverage = structural_coverage(subject.answer, subject.source_ids)
    log.info(
        EVAL_COMPLETED,
        message_id=message_id,
        conversation_id=str(subject.conversation_id),
        relevancy=scores.relevancy,
        faithfulness=scores.faithfulness,
        coverage=coverage.score,
        claims=coverage.claims,
        supported=coverage.supported,
        citations=coverage.citations,
        cited_chunks=len(subject.cited_ids),
        elapsed_ms=elapsed_ms,
    )
