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
    "EVAL_FAILED",
    "EVAL_SKIPPED",
    "EvaluationScores",
    "Evaluator",
    "build_evaluator",
    "structural_coverage",
]

log = structlog.get_logger(__name__)

#: Outside the closed `graph.turn.*` vocabulary R-43(5) fixed, on the `rag.router.*` /
#: `rag.rerank.*` precedent — this job is not a graph node and must not pair spans with one.
EVAL_COMPLETED = "rag.eval.completed"
EVAL_SKIPPED = "rag.eval.skipped"
EVAL_FAILED = "rag.eval.failed"

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
    """FR-EVL-01's two reference-free metrics, judged by `OPENAI_JUDGE_MODEL` (§10.1)."""

    def __init__(self, chat: ChatClient, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._chat = chat
        self._judge: Any | None = None

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
            relevancy=await self._measure(AnswerRelevancyMetric, case, "relevancy"),
            faithfulness=await self._measure(FaithfulnessMetric, case, "faithfulness"),
        )

    async def _measure(self, metric_cls: type, case: Any, name: str) -> float | None:
        if self._judge is None:
            self._judge = _judge_class()(self._chat, model_name=self._chat_model_name())
        try:
            metric = metric_cls(model=self._judge, async_mode=True, include_reason=False)
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

    def _chat_model_name(self) -> str:
        return getattr(self._chat, "judge_model", self._settings.openai.judge_model)


def build_evaluator(chat: ChatClient, settings: Settings | None = None) -> Evaluator:
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
    return DeepEvalEvaluator(chat, settings)
