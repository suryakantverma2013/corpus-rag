"""FR-EVL-01 post-hoc evaluation — the DeepEval judge behind one seam (T-309, R-50).

**langgraph-free**, for the `errors.py` / `generation.py` / `groundedness.py` reason:
`app.rag.graph` calls `apply_strict_msgpack()` at import time, and the evaluation worker
must reach this module without inheriting that.

Two metrics run here, not four. FR-EVL-01 names Answer Relevancy, Faithfulness, Contextual
Precision and Contextual Recall, but the latter two are **reference-based** — DeepEval
requires `expected_output` on the test case for both — and a production chat turn has no
reference answer to supply. R-50 moves those two to an offline golden-set harness (T-312)
and keeps the per-message chips to the two that are reference-free. Inventing an
`expected_output` from the answer itself would make both metrics self-referential and
always high, which is the "launder a proxy as a score" failure R-49(1) already refused.

**The scores this writes are not the gate's `groundedness`** (R-49(1), OI-34). The gate is a
pre-serve, structural, serve/don't-serve control over *whether the model cited its sources*;
Faithfulness is a post-hoc, semantic quality signal about *whether those sources support the
text*. They can disagree about one answer, and that disagreement is the evidence R-49's
revisit trigger needs — which is why :func:`structural_coverage` exists here, so the worker
can log both numbers side by side without either one contaminating the other's surface.
:class:`EvaluationScores` carries exactly two fields, so `groundedness` cannot reach
`messages.evaluation` by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from app.config import Settings, get_settings
from app.rag.generation import split_answer_segments
from app.rag.groundedness import GroundednessReport, assess
from app.services.llm import ChatClient

__all__ = [
    "EVAL_COMPLETED",
    "EVAL_ESCALATED",
    "EVAL_ESCALATION_FAILED",
    "EVAL_FAILED",
    "EVAL_SKIPPED",
    "EvaluationScores",
    "Evaluator",
    "ReferenceScores",
    "build_evaluator",
    "structural_coverage",
]

log = structlog.get_logger(__name__)

#: Outside the closed `graph.turn.*` vocabulary R-43(5) fixed, on the `rag.router.*` /
#: `rag.rerank.*` precedent — this job is not a graph node and must not pair spans with one.
EVAL_COMPLETED = "rag.eval.completed"
EVAL_SKIPPED = "rag.eval.skipped"
EVAL_FAILED = "rag.eval.failed"
#: T-314 two-tier judging. Provenance lives in telemetry rather than in the payload:
#: `EvaluationScores` has exactly two fields by design (R-49(1)/R-50(6)), and which model
#: produced a chip is an operator question, not a user-facing one.
EVAL_ESCALATED = "rag.eval.escalated"
EVAL_ESCALATION_FAILED = "rag.eval.escalation_failed"

#: Judge prompts are short JSON payloads; this bounds a runaway response, not the input.
_JUDGE_MAX_OUTPUT_TOKENS = 2_000  # TBD(§8.4)


@dataclass(frozen=True, slots=True)
class EvaluationScores:
    """What `messages.evaluation` may hold. Two fields, deliberately.

    The field list *is* the schema guard: R-49(1) binds this task not to write the gate's
    structural `groundedness` into `messages.evaluation`, and a dataclass with nowhere to put
    it cannot be talked into it by a later edit to a dict literal.
    """

    relevancy: float | None = None
    faithfulness: float | None = None

    @property
    def empty(self) -> bool:
        """True when nothing was scored — the caller must then write nothing at all."""
        return self.relevancy is None and self.faithfulness is None

    def as_payload(self) -> dict[str, float]:
        """The JSONB payload, omitting whatever failed.

        A partial result is written rather than discarded: FR-EVL-02 renders the chips that
        are present, so one surviving metric is a real chip and a better outcome than none.
        Two decimals because that is exactly what FR-EVL-02 displays (`{Label} {0.00}`) —
        storing more precision than the product shows invites two callers to round it
        differently.
        """
        payload: dict[str, float] = {}
        if self.relevancy is not None:
            payload["relevancy"] = round(self.relevancy, 2)
        if self.faithfulness is not None:
            payload["faithfulness"] = round(self.faithfulness, 2)
        return payload


@dataclass(frozen=True, slots=True)
class ReferenceScores:
    """FR-EVL-01's two **reference-based** metrics (T-312).

    Deliberately a separate type from :class:`EvaluationScores` rather than two more optional
    fields on it. `EvaluationScores` is what `messages.evaluation` may hold, and R-50(1) sent
    these two metrics to an offline harness precisely because a live turn has no
    `expected_output` to compute them from — so a type whose only writer is the harness cannot
    be talked into a per-message column by a later edit, exactly as `EvaluationScores`' own
    two-field list guards against writing the gate's `groundedness` (R-49(1)).

    Never persisted and never rendered per message: FR-ANL-04's Ctx Precision and Ctx Recall
    cells are chat-level and these numbers are corpus-level (R-52).
    """

    ctx_precision: float | None = None
    ctx_recall: float | None = None

    @property
    def empty(self) -> bool:
        return self.ctx_precision is None and self.ctx_recall is None


@runtime_checkable
class Evaluator(Protocol):
    """The seam that makes the judge implementation a swap (R-50).

    `deepeval` is the vendor §10.1 names and the default. The Protocol exists because §10.3(4)
    sanctions moving evaluation out if the dependency ever conflicts, and because a vendor
    whose metrics are five LLM calls behind a class name should not be load-bearing on the
    only interface the worker knows.
    """

    async def score(
        self, *, question: str, answer: str, context: Sequence[str]
    ) -> EvaluationScores:
        """Score one served answer. **Never raises** — a failed metric comes back `None`."""
        ...

    async def score_reference_based(
        self, *, question: str, answer: str, expected: str, context: Sequence[str]
    ) -> ReferenceScores:
        """Score the two metrics that need a reference answer (T-312). **Never raises.**

        On the seam rather than in `evals/` on purpose: `_ChatClientJudge` is what keeps every
        judge call on our own `ChatClient` (R-50(2)), and a second caller reaching for
        `deepeval.metrics` directly would quietly reacquire the vendor's own OpenAI client,
        its telemetry defaults and an error taxonomy we cannot classify. The harness is a
        second *caller* of this module, never a second *user* of that library.
        """
        ...


def structural_coverage(answer: str, source_ids: Sequence[str]) -> GroundednessReport:
    """Recompute the T-308 gate's structural coverage for this answer (R-49(b)).

    The gate's number lives in `RAGState`, which is the checkpointer's, not the worker's — and
    persisting it would be the conflation R-49(1) forbids. Recomputing costs nothing (the gate
    measured ~39 µs and does no I/O) and is exact, because both callers run the *same* two
    pure functions over the same text: R-48(4)'s single parser, then `assess`.

    This is what makes T-309 the named revisit instrument rather than merely a scorer: the
    worker logs this beside Faithfulness, and the correlation between them is the evidence
    that would justify reopening R-49(1)'s decision to skip a pre-serve semantic judge.
    """
    segments, dropped = split_answer_segments(answer, source_ids)
    return assess(segments, markers_dropped=dropped)


# --- DeepEval, driven through our own seam ------------------------------------


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic-generated JSON schema satisfy OpenAI's `strict` structured outputs.

    `_complete_json` sends `strict: True`, which demands that every object set
    `additionalProperties: false` and list **every** property as required, and which rejects
    the annotation keywords pydantic emits freely (`default`, `title`, `$comment`). DeepEval's
    models are written for its own parser and satisfy none of that, so the choice is to
    normalise here or to drop `strict` for this call site — and dropping it would trade a
    guaranteed-parseable judge response for a vendor's schema conventions.

    Optionality is preserved by *union with null* rather than by omission from `required`,
    which is the transform OpenAI documents for exactly this case.
    """
    node = dict(schema)
    node.pop("default", None)
    node.pop("title", None)

    for key in ("$defs", "definitions", "properties"):
        if isinstance(node.get(key), dict):
            node[key] = {name: _strictify(sub) for name, sub in node[key].items()}
    for key in ("items", "additionalItems"):
        if isinstance(node.get(key), dict):
            node[key] = _strictify(node[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(node.get(key), list):
            node[key] = [_strictify(sub) for sub in node[key] if isinstance(sub, dict)]

    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node.get("properties", {}))
    return node


class _ChatClientJudge:
    """A `DeepEvalBaseLLM` whose every call lands on our :class:`ChatClient`.

    This class is the whole reason adopting DeepEval was acceptable. Left to itself the
    library builds its own OpenAI client, which would mean a second place the key lives, an
    error taxonomy `app.rag.errors` cannot classify, no `LLM_EVAL_*` budget, and — worst — a
    test suite that either reaches the network or mocks a vendor's internals. Routed here,
    `LLM_BACKEND=fake` keeps working and the judge is subject to the same seam as every other
    model call in the system.

    The base class is subclassed lazily inside :func:`_judge_class` because importing
    `deepeval` costs ~13 s; nothing that merely imports this module should pay that.
    """

    def __init__(self, chat: ChatClient, *, model_name: str) -> None:
        self._chat = chat
        self._model_name = model_name

    def load_model(self) -> None:
        """No model to load — the seam owns the client."""
        return None

    def get_model_name(self) -> str:
        return self._model_name

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        """Refused on purpose.

        Every metric is constructed with `async_mode=True` and driven with `a_measure`, so
        this is dead in our usage. Implementing it by spinning an event loop would deadlock
        inside the worker's running loop; failing loudly names the real problem instead.
        """
        raise RuntimeError(
            "synchronous judge call: construct DeepEval metrics with async_mode=True"
        )

    async def a_generate(self, *args: Any, **kwargs: Any) -> Any:
        prompt = args[0] if args else kwargs.get("prompt", "")
        schema = kwargs.get("schema")
        if schema is None and len(args) > 1:
            schema = args[1]

        if schema is None:
            # No schema asked for: DeepEval wants prose (a `reason` string). We construct
            # metrics with include_reason=False, so this is defensive rather than expected.
            result = await self._chat.evaluate_json(
                self._messages(prompt),
                schema={"type": "object", "properties": {}},
                schema_name="judge_text",
                max_output_tokens=_JUDGE_MAX_OUTPUT_TOKENS,
            )
            return str(result.data)

        payload = await self._chat.evaluate_json(
            self._messages(prompt),
            schema=_strictify(schema.model_json_schema()),
            schema_name=schema.__name__,
            max_output_tokens=_JUDGE_MAX_OUTPUT_TOKENS,
        )
        return schema(**payload.data)

    @staticmethod
    def _messages(prompt: str) -> list[dict[str, str]]:
        """One `user` message, never a `system` one.

        R-44(3) is not suspended because the caller is a library: a judge prompt embeds the
        answer *and* the retrieved passages, all of it untrusted, and the `system` role is the
        one channel the model is trained to obey. The blast radius here is a wrong score
        rather than a wrong answer, but the rule costs nothing to keep.
        """
        return [{"role": "user", "content": str(prompt)}]


def _judge_class() -> type:
    """Build the `DeepEvalBaseLLM` subclass on first use (the ~13 s import)."""
    from deepeval.models import DeepEvalBaseLLM

    return type("ChatClientJudge", (_ChatClientJudge, DeepEvalBaseLLM), {})


class DeepEvalEvaluator:
    """FR-EVL-01's two reference-free metrics, judged by `OPENAI_JUDGE_MODEL` (§10.1).

    **Two-tier judging (T-314, R-53).** T-312 measured the cheap default returning *false
    zeros on correct answers* — faithfulness 0.00 on an answer that was a verbatim
    restatement of the passage it cited — and FR-EVL-02 renders these numbers as chips a
    user reads. Because the errors cluster in the tail (15 of 18 healthy items scored exactly
    1.00), a score below `EVAL_ESCALATE_BELOW` is re-judged by a stronger model and the
    second score **replaces** the first.

    Two properties keep that honest, and both are asserted in `tests/test_evaluation.py`:

    * **Replace, never `max()`.** A 0.00 the stronger judge confirms stays 0.00. Taking the
      better of two would not be a measurement, it would be a search for the answer we like.
    * **Escalation is off in the T-312 harness** (`escalate=False`). Re-judging only the low
      scores is a *biased estimator* — the aggregate drifts upward because nothing re-rolls a
      1.00 — which is acceptable for a per-message chip and disqualifying for the instrument
      that measures release-over-release quality.
    """

    def __init__(
        self,
        chat: ChatClient,
        settings: Settings | None = None,
        *,
        escalate: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        self._chat = chat
        self._escalate = escalate
        self._judges: dict[str, Any] = {}

    async def score(
        self, *, question: str, answer: str, context: Sequence[str]
    ) -> EvaluationScores:
        """Score both metrics, each guarded independently.

        Independently, because the two share only a judge: a rate limit that kills
        Faithfulness' third call has no bearing on whether Answer Relevancy completed, and
        discarding a metric we already paid for would be a worse outcome than a one-chip row.
        """
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
        from deepeval.test_case import LLMTestCase

        cap = self._settings.eval.max_context_chars
        case = LLMTestCase(
            input=question,
            actual_output=answer,
            retrieval_context=[passage[:cap] for passage in context],
        )
        return EvaluationScores(
            relevancy=await self._scored(AnswerRelevancyMetric, case, "relevancy"),
            faithfulness=await self._scored(FaithfulnessMetric, case, "faithfulness"),
        )

    async def _scored(self, metric_cls: type, case: Any, name: str) -> float | None:
        """One metric on the cheap judge, escalated to the stronger one when it comes back low.

        The escalation is skipped entirely when it would be a no-op — disabled, threshold at
        zero, or both tiers naming the same model — so the common configuration pays exactly
        what it did before T-314.
        """
        base_model = self._chat_model_name()
        score = await self._measure(metric_cls, case, name, model=base_model)

        cut = self._settings.eval.escalate_below
        stronger = self._settings.openai.judge_escalation_model
        if (
            not self._escalate
            or score is None
            or score >= cut
            or cut <= 0.0
            or stronger == base_model
        ):
            return score

        escalated = await self._measure(metric_cls, case, name, model=stronger)
        if escalated is None:
            # The stronger judge failed. Keep the first score rather than dropping the chip —
            # R-50(3)'s fail-open direction, applied to the second tier: a possibly-harsh
            # number is a better outcome than a metric that vanishes.
            log.warning(EVAL_ESCALATION_FAILED, metric=name, score=round(score, 2))
            return score

        log.info(
            EVAL_ESCALATED,
            metric=name,
            base_score=round(score, 2),
            escalated_score=round(escalated, 2),
            base_model=base_model,
            escalation_model=stronger,
            # Whether the second opinion actually moved the chip. This is the number that
            # says, months from now, whether the second tier is still paying for itself.
            changed=abs(escalated - score) >= 0.005,
        )
        return escalated

    async def score_reference_based(
        self, *, question: str, answer: str, expected: str, context: Sequence[str]
    ) -> ReferenceScores:
        """FR-EVL-01's Contextual Precision and Contextual Recall (T-312, R-52).

        The *only* difference from :meth:`score` is `expected_output`, and that difference is
        the whole reason these two metrics could not ship with T-309: both judge the retrieved
        context against an **ideal** answer, so without one they cannot be computed at all —
        not computed badly, not computed. Contextual Precision asks whether the relevant nodes
        rank above the irrelevant ones; Contextual Recall asks whether the context contains
        what the ideal answer needed.

        Both are measured over the context in the order it was given, so the caller must pass
        the ordered grounding set (the reranked top-K the model actually saw) rather than a
        set — a precision metric fed an arbitrary order measures nothing.
        """
        from deepeval.metrics import ContextualPrecisionMetric, ContextualRecallMetric
        from deepeval.test_case import LLMTestCase

        cap = self._settings.eval.max_context_chars
        case = LLMTestCase(
            input=question,
            actual_output=answer,
            expected_output=expected,
            retrieval_context=[passage[:cap] for passage in context],
        )
        return ReferenceScores(
            ctx_precision=await self._measure(ContextualPrecisionMetric, case, "ctx_precision"),
            ctx_recall=await self._measure(ContextualRecallMetric, case, "ctx_recall"),
        )

    async def _measure(
        self, metric_cls: type, case: Any, name: str, *, model: str | None = None
    ) -> float | None:
        judge = self._judge_for(model or self._chat_model_name())
        try:
            metric = metric_cls(model=judge, async_mode=True, include_reason=False)
            # `_log_metric_to_confident=False` is not the same switch as
            # `DEEPEVAL_TELEMETRY_OPT_OUT`: that one governs anonymous usage pings, this one
            # governs shipping the *test case* — question, answer and retrieved passages — to
            # the vendor's cloud. Both are off; only this one carries corpus text.
            return float(
                await metric.a_measure(case, _show_indicator=False, _log_metric_to_confident=False)
            )
        except Exception as exc:  # noqa: BLE001 — R-50: evaluation fails open, per metric
            log.warning(EVAL_FAILED, metric=name, error=str(exc), error_type=type(exc).__name__)
            return None

    def _judge_for(self, model: str) -> Any:
        """One `DeepEvalBaseLLM` per model, built on first use (the ~13 s import).

        Cached per model rather than singly, so a turn that escalates does not rebuild the
        cheap judge afterwards — and so the two tiers cannot accidentally share one adapter
        and send both calls to the same endpoint.
        """
        judge = self._judges.get(model)
        if judge is None:
            judge = _judge_class()(self._chat, model_name=model)
            self._judges[model] = judge
        return judge

    def _chat_model_name(self) -> str:
        return getattr(self._chat, "judge_model", self._settings.openai.judge_model)


def build_evaluator(
    chat: ChatClient, settings: Settings | None = None, *, escalate: bool = True
) -> Evaluator:
    """Build the judge. Returns the :class:`Evaluator` Protocol, never the concrete class.

    There is no `EVAL_BACKEND` knob. The spike that adopted `deepeval` (R-50) confirmed it
    resolves on 3.14 and can be driven entirely through our seam, so a second implementation
    would be speculative code — and a setting whose only valid value is the default is worse
    than none. The Protocol is where a swap would happen if §10.3(4)'s escape is ever taken.
    """
    settings = settings or get_settings()
    if settings.eval.telemetry_opt_out:
        # Set before `deepeval` is imported anywhere: the library reads this at import time,
        # and the worker is a long-lived process where a late opt-out is no opt-out.
        import os

        os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "true")
    return DeepEvalEvaluator(chat, settings, escalate=escalate)
