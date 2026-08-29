"""FR-EVL-01 post-hoc evaluation — the judge, the worker and the skip matrix (T-309, R-50).

The load-bearing assertions here are the *negative* ones. This task's whole risk is writing
the wrong number into a column a user reads: R-49(1) binds it never to persist the gate's
structural `groundedness` as if it were DeepEval's semantic Faithfulness, and the failure
mode is silent — a plausible float in a chip that says something it did not measure. So the
payload's key set is asserted directly, and the invariant is additionally structural (see
`test_scores_cannot_carry_groundedness`).

Everything runs through `LLM_BACKEND=fake`, which is only possible because the judge is
driven through our own `ChatClient` (R-50); a DeepEval left to build its own OpenAI client
would make this file either network-bound or a mock of a vendor's internals.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.message import Message
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.users import UserRepository
from app.rag.evaluation import (
    EVAL_COMPLETED,
    EVAL_ESCALATED,
    EVAL_ESCALATION_FAILED,
    EVAL_SKIPPED,
    DeepEvalEvaluator,
    EvaluationScores,
    build_evaluator,
    structural_coverage,
)
from app.services.jobs import (
    EVALUATE_TASK_NAME,
    NullJobQueue,
    evaluation_idempotency_key,
)
from app.services.llm import ChatUnavailableError, FakeChatClient
from workers.evaluate import evaluate_message

ANSWER = "The refund window is 30 days from purchase. [S1]"
PASSAGE = "Customers may request a refund within 30 days of the purchase date."
QUESTION = "What is the refund window?"


# --- fixtures -----------------------------------------------------------------


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _seed(
    session: AsyncSession,
    *,
    answer: str = ANSWER,
    evaluation: dict | None = None,
    with_question: bool = True,
    chunk_deleted: bool = False,
    cite: bool = True,
) -> tuple[Message, DocumentChunk]:
    """One conversation carrying a question and a cited AI answer."""
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )
    kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
    doc = Document(
        owner_id=user.id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="refunds.pdf",
        storage_uri="s3://corpus/refunds.pdf",
        checksum_sha256=_sha(uuid.uuid4().hex),
        status=DocumentStatus.ACTIVE,
        searchable=True,
        current_version=1,
    )
    if chunk_deleted:
        doc.deleted_at = datetime.now(UTC)
    doc = await DocumentRepository(session).add(doc)

    chunk = DocumentChunk(
        document_id=doc.id,
        document_version=1,
        chunk_index=0,
        chunk_hash=_sha(PASSAGE),
        embedding_fingerprint=_sha(f"fp:{doc.id}:0"),
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        chunk_text=PASSAGE,
        meta={},
    )
    session.add(chunk)
    await session.flush()

    conversation = await ConversationRepository(session).add(
        Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="refunds")
    )
    repo = MessageRepository(session)
    if with_question:
        await repo.add(
            Message(conversation_id=conversation.id, role=MessageRole.USER, content=QUESTION)
        )

    # The T-402 `citations` envelope contract (R-50): resolved segments plus the ordered
    # grounding set the `[S<n>]` markers index into.
    citations = {
        "segments": [{"isCite": True, "chunkId": str(chunk.id)}] if cite else [],
        "source_ids": [str(chunk.id)],
    }
    message = await repo.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.AI,
            content=answer,
            citations=citations,
            evaluation=evaluation,
        )
    )
    await session.flush()
    return message, chunk


def _ctx(session: AsyncSession, chat: FakeChatClient) -> dict:
    """A worker `ctx` bound to the test's transaction, so nothing escapes the rollback."""

    class _Sessionmaker:
        def __call__(self):  # noqa: ANN204
            return _Session()

    class _Session:
        async def __aenter__(self):  # noqa: ANN204
            return session

        async def __aexit__(self, *exc: object) -> bool:
            return False

    return {"sessionmaker": _Sessionmaker(), "chat": chat}


# --- the payload contract -----------------------------------------------------


def test_scores_cannot_carry_groundedness() -> None:
    """R-49(1)(c) enforced structurally, not by review.

    `EvaluationScores` has exactly two fields, so there is nowhere to put the gate's
    structural coverage even by accident. If a later task adds a third field, this test is
    where that decision has to be made deliberately.
    """
    assert {f.name for f in dataclass_fields(EvaluationScores)} == {"relevancy", "faithfulness"}
    assert set(EvaluationScores(relevancy=1.0, faithfulness=1.0).as_payload()) == {
        "relevancy",
        "faithfulness",
    }


def test_payload_omits_a_metric_that_failed() -> None:
    """A partial result is a real chip; discarding it would be a worse outcome than none."""
    assert EvaluationScores(relevancy=0.9).as_payload() == {"relevancy": 0.9}
    assert EvaluationScores().as_payload() == {}
    assert EvaluationScores().empty
    assert not EvaluationScores(faithfulness=0.1).empty


def test_payload_rounds_to_what_the_chip_displays() -> None:
    payload = EvaluationScores(relevancy=0.94449, faithfulness=0.8551).as_payload()
    assert payload == {"relevancy": 0.94, "faithfulness": 0.86}


# --- the judge ----------------------------------------------------------------


async def test_judge_calls_go_through_our_seam() -> None:
    """Every DeepEval LLM call lands on the `ChatClient` — the premise R-50 rests on.

    Five calls: Faithfulness extracts truths, extracts claims and returns verdicts; Answer
    Relevancy extracts statements and returns verdicts. If DeepEval ever opened its own
    client this count would drop and the fake would go unused.
    """
    chat = FakeChatClient()
    scores = await build_evaluator(chat).score(question=QUESTION, answer=ANSWER, context=[PASSAGE])
    assert len(chat.calls) == 5
    assert scores.as_payload() == {"relevancy": 1.0, "faithfulness": 1.0}


async def test_judge_prompts_never_use_the_system_role() -> None:
    """R-44(3) is not suspended because a library composed the prompt.

    A judge prompt embeds the answer *and* the retrieved passages — all untrusted — and
    `system` is the one role the model is trained to obey.
    """
    chat = FakeChatClient()
    await build_evaluator(chat).score(question=QUESTION, answer=ANSWER, context=[PASSAGE])
    roles = {message["role"] for call in chat.calls for message in call}
    assert roles == {"user"}


async def test_a_dead_judge_yields_no_scores_rather_than_raising() -> None:
    """R-50: evaluation fails open. Nothing it does can affect an answer already served."""
    chat = FakeChatClient(error=ChatUnavailableError("judge down"))
    scores = await build_evaluator(chat).score(question=QUESTION, answer=ANSWER, context=[PASSAGE])
    assert scores.empty
    assert scores.as_payload() == {}


async def test_context_is_capped_per_passage() -> None:
    from app.config import get_settings

    cap = get_settings().eval.max_context_chars
    chat = FakeChatClient()
    await build_evaluator(chat).score(question=QUESTION, answer=ANSWER, context=["x" * (cap * 3)])
    longest = max(len(message["content"]) for call in chat.calls for message in call)
    assert longest < cap * 3


# --- the worker ---------------------------------------------------------------


async def test_worker_writes_the_two_metrics(session: AsyncSession) -> None:
    message, _ = await _seed(session)
    chat = FakeChatClient()

    await evaluate_message(_ctx(session, chat), str(message.id))

    await session.refresh(message)
    assert message.evaluation == {"relevancy": 1.0, "faithfulness": 1.0}
    assert "groundedness" not in message.evaluation


async def test_worker_skips_an_already_evaluated_message(session: AsyncSession) -> None:
    """Redelivery safety — a duplicate job must not re-spend five judge calls."""
    existing = {"relevancy": 0.4, "faithfulness": 0.4}
    message, _ = await _seed(session, evaluation=existing)
    chat = FakeChatClient()

    await evaluate_message(_ctx(session, chat), str(message.id))

    await session.refresh(message)
    assert message.evaluation == existing
    assert chat.calls == []


async def test_worker_skips_an_answer_that_cites_nothing(session: AsyncSession) -> None:
    """An abstained or blocked turn cites nothing, so this one condition excludes them.

    It is also the honest boundary: with no cited passage there is no retrieval context, and
    Faithfulness against an empty context is not a low score but a meaningless one.
    """
    message, _ = await _seed(session, cite=False)
    chat = FakeChatClient()

    await evaluate_message(_ctx(session, chat), str(message.id))

    await session.refresh(message)
    assert message.evaluation is None
    assert chat.calls == []


async def test_worker_skips_when_every_cited_document_is_gone(session: AsyncSession) -> None:
    """Scoring against an empty context would return 0.0 and defame a good answer."""
    message, _ = await _seed(session, chunk_deleted=True)
    chat = FakeChatClient()

    await evaluate_message(_ctx(session, chat), str(message.id))

    await session.refresh(message)
    assert message.evaluation is None
    assert chat.calls == []


async def test_worker_skips_a_user_message(session: AsyncSession) -> None:
    message, _ = await _seed(session)
    user_turn = (await MessageRepository(session).list_by_conversation(message.conversation_id))[0]
    chat = FakeChatClient()

    await evaluate_message(_ctx(session, chat), str(user_turn.id))

    assert chat.calls == []


async def test_worker_skips_when_disabled(session: AsyncSession, monkeypatch) -> None:  # noqa: ANN001
    """`EVAL_ENABLED` is a legitimate cost switch: FR-EVL-01 says a response *may* carry
    scores, so "no chips" is a state the requirement itself sanctions."""
    from app.config import get_settings

    settings = get_settings().model_copy(deep=True)
    settings.eval.enabled = False
    message, _ = await _seed(session)
    chat = FakeChatClient()

    ctx = _ctx(session, chat) | {"settings": settings}
    await evaluate_message(ctx, str(message.id))

    await session.refresh(message)
    assert message.evaluation is None
    assert chat.calls == []


async def test_worker_leaves_evaluation_null_when_the_judge_fails(
    session: AsyncSession,
) -> None:
    """Fail open, and on the final attempt without raising.

    arq checks `max_tries` at the top of `run_job`, so a task can never observe its own
    dead-letter; raising here would produce a traceback for an outcome that is by design a
    correct end state.
    """
    message, _ = await _seed(session)
    chat = FakeChatClient(error=ChatUnavailableError("judge down"))

    ctx = _ctx(session, chat) | {"job_try": 99}
    await evaluate_message(ctx, str(message.id))

    await session.refresh(message)
    assert message.evaluation is None


async def test_worker_retries_a_recoverable_judge_failure(session: AsyncSession) -> None:
    """While attempts remain the job defers rather than giving up — a 429 is what retries fix."""
    from arq.worker import Retry

    message, _ = await _seed(session)
    chat = FakeChatClient(error=ChatUnavailableError("judge down"))

    with pytest.raises(Retry):
        await evaluate_message(_ctx(session, chat) | {"job_try": 1}, str(message.id))

    await session.refresh(message)
    assert message.evaluation is None


async def test_worker_is_silent_about_a_missing_message(session: AsyncSession) -> None:
    chat = FakeChatClient()
    await evaluate_message(_ctx(session, chat), str(uuid.uuid4()))
    assert chat.calls == []


# --- R-49(b): the correlation record ------------------------------------------


async def test_completion_event_pairs_faithfulness_with_gate_coverage(
    session: AsyncSession,
) -> None:
    """The reason this task is R-49's named revisit instrument.

    Faithfulness and the T-308 gate's structural coverage measure the same property by
    different means. They are evidence only if recorded *together*, so they ride one event —
    and coverage is recomputed rather than persisted, because `messages.evaluation` is
    DeepEval's alone (R-49(1)(c)).
    """
    message, _ = await _seed(session)
    chat = FakeChatClient()

    with structlog.testing.capture_logs() as logs:
        await evaluate_message(_ctx(session, chat), str(message.id))

    completed = [entry for entry in logs if entry["event"] == EVAL_COMPLETED]
    assert len(completed) == 1
    event = completed[0]
    assert event["faithfulness"] == 1.0
    assert event["coverage"] == 1.0
    assert event["relevancy"] == 1.0


async def test_no_event_carries_answer_or_passage_text(session: AsyncSession) -> None:
    """R-43(5)'s no-payload-text rule applies to every `rag.*` event, not only `graph.turn.*`."""
    message, _ = await _seed(session)
    chat = FakeChatClient()

    with structlog.testing.capture_logs() as logs:
        await evaluate_message(_ctx(session, chat), str(message.id))

    blob = repr(logs)
    assert ANSWER not in blob
    assert PASSAGE not in blob
    assert QUESTION not in blob


async def test_skip_events_name_their_reason(session: AsyncSession) -> None:
    message, _ = await _seed(session, cite=False)
    with structlog.testing.capture_logs() as logs:
        await evaluate_message(_ctx(session, FakeChatClient()), str(message.id))
    skips = [entry for entry in logs if entry["event"] == EVAL_SKIPPED]
    assert [entry["reason"] for entry in skips] == ["no_citations"]


def test_structural_coverage_matches_the_gate() -> None:
    """Both callers run the same two pure functions, so recomputation is exact."""
    report = structural_coverage(ANSWER, ["chunk-a"])
    assert report.score == 1.0
    assert report.citations == 1
    assert structural_coverage("An uncited claim about refunds and windows.", []).score == 0.0


# --- the enqueue seam ---------------------------------------------------------


def test_task_name_matches_the_worker_registration() -> None:
    """A rename silently orphans every queued job — a wire contract, asserted."""
    from workers.main import WorkerSettings

    assert EVALUATE_TASK_NAME in [f.name for f in WorkerSettings.functions]


def test_idempotency_key_survives_redelivery_but_not_regenerate() -> None:
    """FR-MSG-08 Regenerate replaces `content` in place.

    Keyed on the id alone, the second evaluation would be deduped by the broker and the
    message would keep the scores of an answer that no longer exists.
    """
    message_id = uuid.uuid4()
    first = evaluation_idempotency_key(message_id, "the first answer")
    assert first == evaluation_idempotency_key(message_id, "the first answer")
    assert first != evaluation_idempotency_key(message_id, "the regenerated answer")
    assert first != evaluation_idempotency_key(uuid.uuid4(), "the first answer")


async def test_null_queue_accepts_an_evaluation_job() -> None:
    """`QUEUE_BACKEND=none` must not be the reason a chat turn fails."""
    await NullJobQueue().enqueue_evaluate(message_id=uuid.uuid4(), idempotency_key="eval:x:y")


# --- live API (skipped without a key) -----------------------------------------
#
# The fake judge answers every verdict affirmatively, so it can prove the *plumbing* and
# nothing about the *measurement*. Only these tests can tell a metric that discriminates
# from one that returns 1.0 for everything — which would be a chip that lies, and would
# pass every test above.

_LIVE_CONTEXT = [
    "Customers may request a refund within 30 days of the purchase date. Refunds are "
    "issued to the original payment method and take 5-10 business days to appear.",
    "Enterprise plans are billed annually. Downgrades take effect at the end of the "
    "current billing period.",
]
_LIVE_QUESTION = "What is the refund window?"

#: `_LIVE_CONTEXT` plus a passage with **no relation whatsoever** to the question, for the one
#: test that needs an answer which is faithful and useless at the same time (T-313).
#:
#: The distractor has to be genuinely unrelated, and that is the whole repair: the original
#: test quoted the *enterprise billing* passage, and a judge asked whether "billing periods"
#: bears on "the refund window" can reasonably hesitate — which it did, scoring relevancy
#: 0.00-0.67 across runs and failing roughly one run in four however the assertion was phrased.
#: Nothing about a car park bears on a refund window, so the judge has nothing to hesitate
#: over. Fixing the fixture beats adding samples: a noisy measurement made from an ambiguous
#: question is not made honest by taking its median.
_UNRELATED_CONTEXT = [
    *_LIVE_CONTEXT,
    "The staff car park is closed for resurfacing during August; please use the visitor lot.",
]


def _live_evaluator():  # noqa: ANN202
    """An evaluator on the **real** client, bypassing `build_chat_client`.

    `conftest` sets `LLM_BACKEND=fake` for the whole suite, so the factory would hand back
    the fake and these tests would assert the double's own affirmative verdicts — passing
    while proving nothing, which is the failure mode the live-test convention exists to
    prevent. `OpenAIChatClient` is constructed directly for the same reason
    `test_embeddings._live_client` constructs its own.
    """
    from app.config import get_settings
    from app.services.llm import OpenAIChatClient

    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live evaluation tests skipped")
    chat = OpenAIChatClient(settings)
    return build_evaluator(chat, settings), chat


async def test_live_faithfulness_separates_grounded_from_fabricated() -> None:
    """The measurement itself, which no mocked test can vouch for.

    Measured 2026-08-01 on `gpt-4o-mini`: grounded **1.0**, fabricated **0.0** — clean
    separation, and the reason the chip is worth showing a user.

    **Asserted on a median of three and on the GAP, not on an absolute floor (T-730,
    2026-08-29).** The original form pinned `grounded >= 0.8` on a single draw and went red
    on a full run at **0.667**, then passed 3/3 on re-run — so that number was a probability
    wearing a property's clothes, which is what T-313 recorded for the sibling test below.
    The name says what the claim is: *separates*. A gap is that claim; a floor on one side
    of it is not.

    **Why a median here and a fixture repair there.** T-313's note above `_UNRELATED_CONTEXT`
    warns that a noisy measurement drawn from an *ambiguous question* is not made honest by
    taking its median — so ambiguity was ruled out first rather than assumed away. Seven
    consecutive live draws on `gpt-4o` scored grounded `1.0` and fabricated `0.0` **every
    time**, gap `1.0`, no `None`s. The fixture is clean and the 0.667 is a rare bad draw, so
    the median is the right instrument here, where there it would have papered over a real
    defect in the question.

    **Why the gap threshold is loose on purpose.** R-53 measured this judge returning false
    *lows* on correct answers, and the effect **scales with grounding-set size** — this
    fixture is two passages where production is eight (R-47's `RERANK_TOP_K`). A threshold
    tuned tight against a measured gap of 1.0 would be tuned against the easiest shape this
    judge ever sees. `0.4` is the sibling's constant, it still fails a judge that answers
    `1.0` to everything (gap 0), and it leaves room for the low grounded draws R-53 predicts.

    **What is deliberately NOT relaxed:** `fabricated <= 0.4`. That side never flaked — 0.0
    in 7 of 7 — and dropping it would let both scores drift upward together while the gap
    still passed, which is a judge that has stopped detecting fabrication. Lowering `0.8`
    instead of replacing it would have traded a flake for a test that no longer discriminates.
    """
    import statistics

    evaluator, chat = _live_evaluator()
    samples: list[tuple[float, float]] = []
    try:
        for _ in range(3):
            grounded = await evaluator.score(
                question=_LIVE_QUESTION,
                answer=(
                    "The refund window is 30 days from the purchase date. [S1] Refunds go "
                    "back to the original payment method and take 5-10 business days. [S1]"
                ),
                context=_LIVE_CONTEXT,
            )
            fabricated = await evaluator.score(
                question=_LIVE_QUESTION,
                answer=(
                    "The refund window is 90 days from purchase. [S1] Refunds are issued as "
                    "store credit only and are processed instantly. [S1]"
                ),
                context=_LIVE_CONTEXT,
            )
            # `None` is a *sanctioned* outcome (R-50(3)) — each metric fails open, and a
            # judge call that overruns `_JUDGE_MAX_OUTPUT_TOKENS` comes back empty. A draw
            # is usable only if both halves scored, because the gap needs the pair.
            if grounded.faithfulness is not None and fabricated.faithfulness is not None:
                samples.append((grounded.faithfulness, fabricated.faithfulness))
    finally:
        await chat.aclose()

    assert len(samples) >= 2, f"the judge returned too few usable pairs: {len(samples)}/3"
    grounded_score = statistics.median(g for g, _ in samples)
    fabricated_score = statistics.median(f for _, f in samples)
    report = (
        f"median grounded={grounded_score:.2f} fabricated={fabricated_score:.2f}"
        f" from {[(round(g, 2), round(f, 2)) for g, f in samples]}"
    )
    # The stable side, and what keeps the test discriminating: an invented answer must
    # actually score low, or both scores could drift up together and still separate.
    assert fabricated_score <= 0.4, f"a fabricated answer was not scored unfaithful — {report}"
    # The claim the test's name makes. Not a floor on the grounded side: R-53's false lows
    # are a known property of this judge, so a floor asserts it is better than it is.
    assert grounded_score - fabricated_score >= 0.4, (
        f"faithfulness did not separate grounded from fabricated — {report}"
    )


async def test_live_relevancy_is_not_a_second_faithfulness() -> None:
    """An answer can be perfectly faithful and completely useless.

    Measured 2026-08-01: an accurate quotation of the *wrong* passage scores faithfulness
    **1.0** and relevancy **0.0**. That is the evidence the two chips are not redundant —
    and it is why FR-EVL-01 wants both rather than one.

    **Asserted on a median of three, not on one sample (T-313, 2026-08-02), and that is the
    whole point of the repair.** The original form pinned `relevancy <= 0.4` on a single run.
    It held under `gpt-4o-mini` — measured `[0, 0, 0, 0, 0]` — and went red the moment
    `OPENAI_JUDGE_MODEL` was raised, because `gpt-4o` is *noisier on this two-passage context*
    even though it is the better judge on the eight-passage shape production actually uses
    (five runs: relevancy `[0.00, 0.50, 0.00, 0.00, 0.50]`, and the run that failed CI scored
    0.67 **above** its own faithfulness of 0.50). So no single-sample assertion can carry this
    claim, however it is phrased: on a bad draw the judge is simply wrong about both metrics
    at once. A median over three draws is the smallest honest instrument for a property that
    is statistical, and it is the T-313 lesson in one line — *a live assertion about model
    behaviour is a probability, not a property*, so measure it like one.
    """
    import statistics

    evaluator, chat = _live_evaluator()
    samples: list[tuple[float, float]] = []
    try:
        for _ in range(3):
            scores = await evaluator.score(
                question=_LIVE_QUESTION,
                answer="The staff car park is closed for resurfacing during August. [S3]",
                context=_UNRELATED_CONTEXT,
            )
            # A `None` is a *sanctioned* outcome, not a failure: R-50(3) makes each metric
            # fail open, and a judge call that overruns `_JUDGE_MAX_OUTPUT_TOKENS` comes back
            # empty — observed live. Asserting not-None per sample would make this test red
            # for the evaluator behaving exactly as ruled, so an unusable draw is discarded
            # and the run needs a majority of usable ones instead.
            if scores.relevancy is not None and scores.faithfulness is not None:
                samples.append((scores.relevancy, scores.faithfulness))
    finally:
        await chat.aclose()

    assert len(samples) >= 2, f"the judge returned too few usable scores: {len(samples)}/3"
    relevancy = statistics.median(r for r, _ in samples)
    faithfulness = statistics.median(f for _, f in samples)
    report = (
        f"median relevancy={relevancy:.2f} faithfulness={faithfulness:.2f}"
        f" from {[(round(r, 2), round(f, 2)) for r, f in samples]}"
    )
    # Faithful: the answer quotes the context accurately, whichever passage it chose.
    assert faithfulness >= 0.5, f"an accurate quotation scored unfaithful — {report}"
    # Useless: it answers a different question than the one asked. The *gap* is the claim.
    assert faithfulness - relevancy >= 0.4, (
        f"the two metrics did not separate on an answer that is accurate but useless — {report}"
    )


async def test_live_the_gate_and_the_judge_can_disagree() -> None:
    """OI-34's scenario, reproduced rather than hypothesised — and R-49(b)'s first corpus point.

    A fabricated answer that cites a real, in-scope, ACTIVE chunk scores **1.00** at the
    T-308 gate and **0.0** here. R-49(1) named exactly this limitation ("an answer citing a
    real chunk for a wrong claim passes here") and deferred the evidence to this task; this
    is that evidence, measured on 2026-08-01.

    The test asserts the *disagreement is possible*, not which side is right — that is
    precisely what OI-34 settles by scoping them to different questions.
    """
    evaluator, chat = _live_evaluator()
    fabricated = (
        "The refund window is 90 days from purchase. [S1] Refunds are issued as store "
        "credit only and are processed instantly. [S1]"
    )
    try:
        scores = await evaluator.score(
            question=_LIVE_QUESTION, answer=fabricated, context=_LIVE_CONTEXT
        )
    finally:
        await chat.aclose()

    coverage = structural_coverage(fabricated, ["chunk-1", "chunk-2"])
    assert coverage.score == 1.0, "the structural gate passes a fully cited answer"
    assert scores.faithfulness is not None and scores.faithfulness <= 0.4
    assert coverage.score - scores.faithfulness >= 0.5


# --- two-tier judging (T-314, R-53) -------------------------------------------
#
# These drive `_scored` through a scripted `_measure`, which is the seam that matters: the
# decision under test is *when to ask a second judge and whose answer to keep*, not whether
# DeepEval can compute a metric. Patching one level lower would test the vendor.


def _scripted(evaluator, scores: list[float | None]) -> list[str | None]:
    """Replace `_measure` with a queue of results; return the list of models it was asked for."""
    asked: list[str | None] = []
    queue = list(scores)

    async def fake_measure(metric_cls, case, name, *, model=None):  # noqa: ANN001, ANN202, ARG001
        asked.append(model)
        return queue.pop(0)

    evaluator._measure = fake_measure  # type: ignore[method-assign]  # noqa: SLF001
    return asked


async def test_a_low_score_is_re_judged_by_the_stronger_model() -> None:
    """T-312 measured the cheap judge returning 0.00 on a verbatim-grounded answer.

    FR-EVL-02 renders that as a chip, so the low tail — and only the low tail — gets a second
    opinion.
    """
    settings = get_settings()
    chat = FakeChatClient()
    evaluator = DeepEvalEvaluator(chat, settings)
    asked = _scripted(evaluator, [0.0, 1.0, 1.0])

    scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert scores.relevancy == 1.0, "the escalated score must replace the low one"
    # The base tier is whatever the *client* calls its judge — the fake names itself, a real
    # `OpenAIChatClient` names `OPENAI_JUDGE_MODEL` — and only the second tier is a settings
    # lookup. Asserting the pair is what shows the two calls went to different models.
    assert asked[:2] == [chat.judge_model, settings.openai.judge_escalation_model]


async def test_a_healthy_score_is_never_re_judged() -> None:
    """The whole economy of the design: 15 of 18 healthy items score exactly 1.00."""
    evaluator = DeepEvalEvaluator(FakeChatClient(), get_settings())
    asked = _scripted(evaluator, [1.0, 1.0])

    await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert len(asked) == 2, "one call per metric, no escalation"


async def test_the_escalated_score_replaces_rather_than_wins() -> None:
    """**Not `max()`.** A 0.00 the stronger judge confirms stays 0.00.

    Taking the better of two would stop being a measurement and become a search for the
    answer we prefer — the "launder a proxy as a score" failure R-49(1) refused, one level up.
    """
    evaluator = DeepEvalEvaluator(FakeChatClient(), get_settings())
    _scripted(evaluator, [0.5, 0.0, 1.0])

    scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert scores.relevancy == 0.0


async def test_a_failed_escalation_keeps_the_first_score() -> None:
    """R-50(3)'s fail-open direction applied to the second tier: a harsh chip beats no chip."""
    evaluator = DeepEvalEvaluator(FakeChatClient(), get_settings())
    _scripted(evaluator, [0.2, None, 1.0])

    with structlog.testing.capture_logs() as logs:
        scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert scores.relevancy == 0.2
    assert [entry for entry in logs if entry["event"] == EVAL_ESCALATION_FAILED]


async def test_the_escalation_is_recorded_in_telemetry_not_in_the_payload() -> None:
    """Which model produced a chip is an operator question (R-49(1)/R-50(6)).

    `EvaluationScores` still has exactly two fields, so provenance has nowhere to leak into
    `messages.evaluation` — it goes to the log stream instead.
    """
    evaluator = DeepEvalEvaluator(FakeChatClient(), get_settings())
    _scripted(evaluator, [0.0, 1.0, 1.0])

    with structlog.testing.capture_logs() as logs:
        scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    escalated = [entry for entry in logs if entry["event"] == EVAL_ESCALATED]
    assert len(escalated) == 1
    assert escalated[0]["metric"] == "relevancy"
    assert escalated[0]["base_score"] == 0.0
    assert escalated[0]["escalated_score"] == 1.0
    assert escalated[0]["changed"] is True
    assert set(scores.as_payload()) <= {"relevancy", "faithfulness"}


async def test_escalation_can_be_switched_off_by_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EVAL_ESCALATE_BELOW=0` is the off switch, which is why there is no separate bool."""
    settings = get_settings()
    monkeypatch.setattr(settings.eval, "escalate_below", 0.0)
    evaluator = DeepEvalEvaluator(FakeChatClient(), settings)
    asked = _scripted(evaluator, [0.0, 0.0])

    scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert len(asked) == 2
    assert scores.relevancy == 0.0


async def test_escalation_is_skipped_when_both_tiers_name_one_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a shop running one model everywhere would pay twice for the same answer."""
    settings = get_settings()
    monkeypatch.setattr(settings.openai, "judge_escalation_model", FakeChatClient().judge_model)
    evaluator = DeepEvalEvaluator(FakeChatClient(), settings)
    asked = _scripted(evaluator, [0.0, 0.0])

    await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert len(asked) == 2


async def test_the_offline_harness_never_escalates() -> None:
    """R-53: re-judging only the low scores is a biased estimator.

    Acceptable for a per-message chip, disqualifying for the instrument T-312 uses to compare
    one release against the next — nothing ever re-rolls a 1.00, so the aggregate drifts up.
    """
    evaluator = build_evaluator(FakeChatClient(), get_settings(), escalate=False)
    asked = _scripted(evaluator, [0.0, 0.0])

    scores = await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert len(asked) == 2
    assert scores.relevancy == 0.0


# --- runtime model selection (T-611, R-83) ------------------------------------


async def test_the_judge_follows_the_operators_selection() -> None:
    """Both tiers come from the selection when one is supplied.

    The base tier is asserted against the override rather than `chat.judge_model`: the fake
    exposes that property so tests can see which tier was asked, and an implementation that
    consulted it first would make the override unreachable in exactly the configuration every
    test here runs under.
    """
    from app.services.model_selection import ModelSelection

    chat = FakeChatClient()
    evaluator = DeepEvalEvaluator(
        chat,
        get_settings(),
        models=ModelSelection(
            chat="c",
            router="r",
            rerank="k",
            judge="judge-base",
            judge_escalation="judge-strong",
            embedding="e",
        ),
    )
    asked = _scripted(evaluator, [0.0, 1.0, 1.0])

    await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert asked[:2] == ["judge-base", "judge-strong"]
    assert chat.judge_model not in asked, "the client's own default must not leak in"


async def test_moving_the_judge_alone_does_not_silently_arm_escalation() -> None:
    """Escalation is dormant only while the two tiers name the same model.

    So a selection that repointed `judge` while leaving `judge_escalation` at the environment
    value would buy a second judge call per metric that nobody asked for. Reading both from
    the same selection is what prevents it — this fails if `_scored` mixes sources.
    """
    from app.services.model_selection import ModelSelection

    evaluator = DeepEvalEvaluator(
        FakeChatClient(),
        get_settings(),
        models=ModelSelection(
            chat="c",
            router="r",
            rerank="k",
            judge="same-model",
            judge_escalation="same-model",
            embedding="e",
        ),
    )
    asked = _scripted(evaluator, [0.0, 0.0])

    await evaluator.score(question=QUESTION, answer=ANSWER, context=[PASSAGE])

    assert asked == ["same-model", "same-model"], "one call per metric, no escalation"
