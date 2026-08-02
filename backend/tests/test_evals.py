"""The offline golden-set harness: corpus rules, aggregation, and hygiene (T-312, R-52).

**No database, no network, no judge.** Everything under test here is the part of the harness
that decides what a number *means* — which items run, which metrics apply to which band, what
an absent score does to an average, and when the structural gate and the semantic judge are
recorded as disagreeing. The paid half (seeding, the pipeline, DeepEval) is exercised by
running `python -m evals.run`, which is a deliberate act with a bill attached.

The one test here that guards a *ruling* rather than arithmetic is
:func:`test_the_harness_never_imports_deepeval_itself`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from evals.corpus import CorpusError, load_golden_set, select
from evals.pipeline import SeededCorpus, TurnResult, cited_passage_ids, recall_at_k
from evals.report import FAITHFULNESS_LOW, ItemScore, aggregate, mean_present, render_text

# --- fixtures ------------------------------------------------------------------

_MINIMAL = {
    "version": 1,
    "topics": ["t"],
    "passages": [
        {"id": "p1", "topic": "t", "filename": "f.md", "locator": "A", "text": "one"},
        {"id": "p2", "topic": "t", "filename": "f.md", "locator": "B", "text": "two"},
    ],
    "questions": [
        {
            "id": "q1",
            "band": "answerable",
            "question": "?",
            "expected_output": "one",
            "supporting_passage_ids": ["p1"],
        },
        {"id": "q2", "band": "unanswerable", "question": "?", "expected_output": "no"},
        {
            "id": "q3",
            "band": "near_miss",
            "question": "?",
            "expected_output": "no",
            "distractor_passage_ids": ["p2"],
        },
    ],
}


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _item(**overrides: object) -> ItemScore:
    fields: dict[str, object] = {"question_id": "q", "band": "answerable", "question": "?"}
    fields.update(overrides)
    return ItemScore(**fields)  # type: ignore[arg-type]


# --- the shipped corpus --------------------------------------------------------


def test_the_committed_golden_set_is_valid() -> None:
    """The corpus is data, and data rots silently — so loading it is a test, not a script step."""
    golden = load_golden_set()
    assert len(golden.passages) >= 40, (
        "a corpus smaller than RERANK_TOP_K*5 makes top-K unselective"
    )
    assert golden.by_band("answerable")
    assert golden.by_band("unanswerable")
    assert golden.by_band("near_miss"), "the OI-34 band is what makes the R-49 correlation possible"


def test_every_near_miss_names_the_passage_that_tempts_it() -> None:
    """A near-miss with no distractor is just an unanswerable question with a longer label."""
    for question in load_golden_set().by_band("near_miss"):
        assert question.distractor_passage_ids, question.id


def test_only_the_answerable_band_is_reference_scored() -> None:
    """R-52: Contextual Recall over an ideal answer of "the documents do not say" is vacuous."""
    golden = load_golden_set()
    assert all(q.reference_scored for q in golden.by_band("answerable"))
    assert not any(q.reference_scored for q in golden.by_band("unanswerable"))
    assert not any(q.reference_scored for q in golden.by_band("near_miss"))


# --- corpus validation ---------------------------------------------------------


def test_a_valid_corpus_loads(tmp_path: Path) -> None:
    golden = load_golden_set(_write(tmp_path, _MINIMAL))
    assert golden.passage_ids == {"p1", "p2"}
    assert [q.id for q in golden.questions] == ["q1", "q2", "q3"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["passages"].append(dict(d["passages"][0])), "duplicate passage"),
        (lambda d: d["questions"].append(dict(d["questions"][0])), "duplicate question"),
        (
            lambda d: d["questions"][0].__setitem__("supporting_passage_ids", ["nope"]),
            "unknown passages",
        ),
        (lambda d: d["questions"][0].__setitem__("supporting_passage_ids", []), "names no support"),
        (
            lambda d: d["questions"][1].__setitem__("supporting_passage_ids", ["p1"]),
            "must name no supporting passage",
        ),
        (lambda d: d["questions"][0].__setitem__("band", "tricky"), "unknown band"),
        (lambda d: d.__setitem__("passages", []), "no passages"),
        (lambda d: d.__setitem__("questions", []), "no questions"),
    ],
)
def test_an_inconsistent_corpus_is_refused_at_load(tmp_path: Path, mutate, match: str) -> None:  # noqa: ANN001
    """Every rule here corresponds to a way the report would lie rather than fail.

    An unknown `supporting_passage_ids` is the sharpest: recall@k would score against an id
    nothing can return, which reads as a retrieval regression and is a typo.
    """
    payload = json.loads(json.dumps(_MINIMAL))
    mutate(payload)
    with pytest.raises(CorpusError, match=match):
        load_golden_set(_write(tmp_path, payload))


def test_a_limited_run_covers_every_band(tmp_path: Path) -> None:
    """`--limit 3` must not mean "three answerable questions".

    The cheap run is the one most likely to be the only one anybody does, so it has to
    exercise the decline paths too.
    """
    golden = load_golden_set(_write(tmp_path, _MINIMAL))
    assert {q.band for q in select(golden, limit=3)} == {
        "answerable",
        "unanswerable",
        "near_miss",
    }


def test_a_band_filter_selects_only_that_band(tmp_path: Path) -> None:
    golden = load_golden_set(_write(tmp_path, _MINIMAL))
    assert [q.id for q in select(golden, bands=["near_miss"])] == ["q3"]


# --- recall@k, the deterministic control ---------------------------------------


def _turn(**overrides: object) -> TurnResult:
    fields: dict[str, object] = {"question_id": "q", "answer": "a"}
    fields.update(overrides)
    return TurnResult(**fields)  # type: ignore[arg-type]


def test_recall_at_k_measures_the_authored_support(tmp_path: Path) -> None:
    golden = load_golden_set(_write(tmp_path, _MINIMAL))
    answerable = golden.by_band("answerable")[0]

    assert recall_at_k(_turn(grounding_passage_ids=("p1", "p2")), answerable) == 1.0
    assert recall_at_k(_turn(grounding_passage_ids=("p2",)), answerable) == 0.0


def test_recall_at_k_is_undefined_where_nothing_supports_the_question(tmp_path: Path) -> None:
    """Not 0.0 — there is nothing to recall, and a zero would drag the band average down."""
    golden = load_golden_set(_write(tmp_path, _MINIMAL))
    assert (
        recall_at_k(_turn(grounding_passage_ids=("p1",)), golden.by_band("unanswerable")[0]) is None
    )


def test_citations_resolve_back_to_passage_ids() -> None:
    chunk = uuid.uuid4()
    corpus = SeededCorpus(
        owner_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        chunk_by_passage={"p1": chunk},
        passage_by_chunk={chunk: "p1"},
        text_by_chunk={chunk: "one"},
    )
    result = _turn(cited_chunk_ids=(str(chunk), "not-a-uuid", str(uuid.uuid4())))
    assert cited_passage_ids(result, corpus) == ("p1",)


# --- aggregation ---------------------------------------------------------------


def test_a_missing_metric_is_excluded_rather_than_counted_as_zero() -> None:
    """The FR-EVL-02 chip rule applied to an average: absent is absent, not 0.00."""
    assert mean_present([1.0, None, 0.0]) == 0.5
    assert mean_present([None, None]) is None


def test_bands_are_averaged_separately() -> None:
    """One band is designed to score zero, so a single mean would move with the band mix."""
    report = aggregate(
        [
            _item(band="answerable", faithfulness=1.0),
            _item(band="answerable", faithfulness=0.8),
            _item(band="unanswerable", faithfulness=0.0),
        ]
    )
    assert report.by_band["answerable"]["faithfulness"] == pytest.approx(0.9)
    assert report.by_band["unanswerable"]["faithfulness"] == 0.0


def test_an_errored_item_is_counted_but_never_averaged() -> None:
    report = aggregate([_item(faithfulness=1.0), _item(error="boom", faithfulness=None)])
    assert report.counts["ok"] == 1
    assert report.counts["errored"] == 1
    assert report.by_band["answerable"]["faithfulness"] == 1.0


def test_a_band_with_no_items_is_absent_rather_than_empty() -> None:
    report = aggregate([_item(band="answerable", relevancy=1.0)])
    assert "unanswerable" not in report.by_band


# --- the R-49 revisit evidence -------------------------------------------------


def test_a_gate_pass_with_low_faithfulness_is_what_a_pre_serve_judge_would_catch() -> None:
    """OI-34's scenario, as a counter: cited a real passage, unsupported by it.

    This is the number R-49(1) said only T-312 could produce — the frequency of the failure
    the structural gate provably cannot see.
    """
    report = aggregate(
        [
            _item(question_id="bad", gate_verdict="pass", faithfulness=0.0),
            _item(question_id="good", gate_verdict="pass", faithfulness=1.0),
        ]
    )
    assert report.disagreements["gate_passed_low_faithfulness"] == ["bad"]
    assert report.disagreements["agreement"] == 1


def test_a_gate_abstain_with_high_faithfulness_is_what_it_would_cost() -> None:
    """The other direction, and it must be counted: a control that refuses good answers is not
    an improvement, however many bad ones it catches."""
    report = aggregate(
        [
            _item(question_id="refused", gate_verdict="abstain", faithfulness=1.0),
            _item(question_id="retried", gate_verdict="retry", faithfulness=0.9),
        ]
    )
    assert report.disagreements["gate_abstained_high_faithfulness"] == ["refused", "retried"]


def test_an_item_the_judge_could_not_score_is_not_comparable() -> None:
    """A failed judge call must not silently become evidence for either side."""
    report = aggregate([_item(gate_verdict="pass", faithfulness=None)])
    assert report.disagreements["comparable_items"] == 0
    assert report.disagreements["agreement"] == 0


def test_the_low_cut_is_a_reporting_threshold_not_a_product_one() -> None:
    """Nothing serves or refuses on it; it exists so the two tables read on one scale."""
    report = aggregate([_item(gate_verdict="pass", faithfulness=FAITHFULNESS_LOW)])
    assert report.disagreements["gate_passed_low_faithfulness"] == []


# --- rendering -----------------------------------------------------------------


def test_the_report_renders_when_every_metric_is_missing() -> None:
    """A run with a dead judge must still print its pipeline results rather than crash."""
    text = render_text(aggregate([_item(gate_verdict="pass")], meta={"judged": False}))
    assert "T-312 golden-set run" in text
    assert "—" in text


def test_the_artifact_round_trips_through_json() -> None:
    report = aggregate([_item(relevancy=0.5, cited_passage_ids=("p1",))], meta={"judged": True})
    restored = json.loads(json.dumps(report.as_dict(), default=str))
    assert restored["items"][0]["relevancy"] == 0.5
    assert restored["meta"]["judged"] is True


# --- hygiene -------------------------------------------------------------------


def test_the_harness_never_imports_deepeval_itself() -> None:
    """R-52 / R-50(2): the seam owns the vendor, and this is what keeps that true.

    `app.rag.evaluation` is where every judge call is routed through our own `ChatClient`,
    where telemetry is opted out at import *and* per measurement, and where the ~13 s import is
    deferred. A harness that reached for `deepeval.metrics` directly would quietly reacquire
    the vendor's own OpenAI client and all three of those properties would silently lapse —
    with nothing failing, which is why this is a test rather than a convention.
    """
    import ast

    offenders: list[str] = []
    for path in Path(__file__).resolve().parent.parent.joinpath("evals").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # Imports only. A *mention* is fine and expected — `evals.corpus` explains why the
            # golden set is JSON rather than YAML by naming the package that would have
            # supplied the parser, and a test that failed on prose would be one nobody could
            # write a docstring around.
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == "deepeval" for a in node.names
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "deepeval":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_harness_disables_judge_escalation() -> None:
    """R-53: escalation belongs to the per-message chip, never to the measuring instrument.

    Re-judging only the *low* scores is a biased estimator — nothing ever re-rolls a 1.00, so
    the aggregate drifts upward and stops being comparable release over release. Asserted on
    the call site rather than on behaviour, because the failure is silent: an escalating
    harness still produces numbers, just slightly flattering ones.
    """
    import ast

    source = Path(__file__).resolve().parent.parent / "evals" / "run.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_evaluator"
    ]
    assert calls, "the harness no longer builds an evaluator — this guard needs rewriting"
    for call in calls:
        escalate = [kw for kw in call.keywords if kw.arg == "escalate"]
        assert escalate, f"build_evaluator at line {call.lineno} must pass escalate=False"
        assert escalate[0].value.value is False
