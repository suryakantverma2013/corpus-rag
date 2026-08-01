"""FR-CIT-06(4) groundedness measurement and the FR-RET-05 retry probe (T-308, R-49).

No database and no model, and that is the design rather than the test setup: R-49 makes the
gate a pure function of checkpointed state, so everything it decides is decidable here.

The weight sits on two rules, because they are where a naive implementation goes wrong in a
way nothing downstream would report. **Marker attachment** decides which sentence a citation
vouches for, so an off-by-one span turns a healthy answer into an abstention or the reverse.
The **substantive-claim filter** is what keeps the score sane at all — without it every
"In summary:" and bare heading counts as an unsupported claim, and a perfectly grounded answer
fails a threshold that then gets blamed for it.

Every input here is built through `split_answer_segments`, never by hand-assembling
`AnswerSegment`s: R-48(4) makes that the one parser, and a test that bypasses it would prove
the metric works on segments the product never produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import GateSettings, RouterSettings, Settings
from app.rag import groundedness as groundedness_module
from app.rag.generation import split_answer_segments
from app.rag.groundedness import GroundednessReport, assess, hypothetical_probe

IDS = ["chunk-1", "chunk-2", "chunk-3"]


def _settings(**gate: object) -> Settings:
    return Settings(gate=GateSettings(**gate))  # type: ignore[arg-type]


def _assess(answer: str, *, ids: list[str] | None = None, **gate: object) -> GroundednessReport:
    """Measure `answer` end to end, exactly as the `gate` node does."""
    segments, dropped = split_answer_segments(answer, IDS if ids is None else ids)
    return assess(segments, markers_dropped=dropped, settings=_settings(**gate))


# --- coverage arithmetic ------------------------------------------------------


def test_every_claim_cited_scores_one() -> None:
    report = _assess(
        "The policy requires two approvals. [S1] Requests expire after thirty days. [S2]"
    )
    assert report.score == 1.0
    assert (report.claims, report.supported, report.citations) == (2, 2, 2)
    assert report.reason is None


def test_no_markers_at_all_scores_zero() -> None:
    """The fabrication signature: an answer written from pre-training has nothing to cite."""
    report = _assess("The policy requires two approvals. Requests expire after thirty days.")
    assert report.score == 0.0
    assert report.citations == 0
    assert report.reason == "no_citations"


def test_partial_coverage_scores_the_fraction() -> None:
    report = _assess(
        "The policy requires two approvals. [S1] "
        "Requests expire after thirty days. "
        "Escalation is manual and undocumented."
    )
    assert report.score == pytest.approx(1 / 3)
    assert (report.claims, report.supported) == (3, 1)
    assert report.reason == "partial_coverage"


def test_citation_ids_are_the_distinct_chunks_in_first_citation_order() -> None:
    report = _assess(
        "Approvals are required for every release. [S2] "
        "They expire after thirty days. [S1] "
        "The same rule covers hotfixes. [S2]"
    )
    assert report.citation_ids == ("chunk-2", "chunk-1")
    assert report.citations == 3  # three markers, two distinct chunks


def test_score_never_leaves_the_unit_interval() -> None:
    """Two markers on one sentence must not score 2.0 — `supported` is a set of spans."""
    report = _assess("Both documents agree on the approval rule. [S1] [S2]")
    assert report.score == 1.0
    assert (report.claims, report.supported, report.citations) == (1, 1, 2)


def test_markers_dropped_is_carried_through_for_telemetry() -> None:
    """`[S9]` against three sources is already dropped from the text by R-48(6); the count is
    the only trace left, and a model that regularly does it is a prompt problem."""
    report = _assess("The policy requires two approvals. [S9] It expires after thirty days. [S2]")
    assert report.markers_dropped == 1
    # Dropped means dropped: it is neither counted as a citation nor resolved to a chunk, which
    # is what FR-CIT-06(2) forbids.
    assert report.citations == 1
    assert report.citation_ids == ("chunk-2",)


# --- marker attachment --------------------------------------------------------


def test_a_marker_after_the_period_supports_the_sentence_it_follows() -> None:
    report = _assess("The policy requires two approvals. [S1] Requests expire in thirty days.")
    assert (report.claims, report.supported) == (2, 1)


def test_a_marker_inside_a_sentence_supports_that_sentence() -> None:
    report = _assess("The policy requires two approvals [S1] before any release goes out.")
    assert report.score == 1.0
    assert (report.claims, report.supported) == (1, 1)


def test_a_trailing_marker_covers_the_run_of_claims_it_ends() -> None:
    """The rule the live pass forced (T-308, `gpt-4o`, 2026-07-31).

    A per-sentence rule scored **0.25–0.5** on answers that were perfectly grounded, because a
    real model writes two or three sentences from one passage and puts one marker at the end of
    the paragraph — the ordinary prose convention, and what `SYSTEM_PROMPT` actually elicits.
    Abstaining on those is the false-positive failure R-44 calls worse than no control.
    """
    report = _assess(
        "If an approval request receives no response, it expires after thirty days. "
        "Once expired, it must be raised again from scratch as there is no way to extend it. "
        "[S1]"
    )
    assert report.score == 1.0
    assert (report.claims, report.supported, report.citations) == (2, 2, 1)


def test_a_marker_cannot_reach_back_across_a_blank_line() -> None:
    """Blocks bound the reach. A separate paragraph is a separate point, and letting one
    marker cover the whole answer would make the metric unable to fail anything that cites
    once."""
    report = _assess(
        "Escalation is handled manually by the on-call engineer.\n\n"
        "Audit records are retained for seven years. [S1]"
    )
    assert (report.claims, report.supported) == (2, 1)
    assert report.score == 0.5


def test_each_paragraph_carries_its_own_marker() -> None:
    """The shape a multi-part answer actually takes: one marker per paragraph, each covering
    its own block."""
    report = _assess(
        "The approval process requires two independent sign-offs from separate people. "
        "They are recorded in the release ticket. [S1]\n\n"
        "A request that goes unanswered expires after thirty days. "
        "It must then be raised again from scratch. [S2]"
    )
    assert report.score == 1.0
    assert (report.claims, report.supported, report.citations) == (4, 4, 2)


def test_a_marker_after_a_bulleted_list_covers_the_list() -> None:
    """The live answer that scored 0.25 under the per-sentence rule: three bullets from one
    passage, one marker at the end."""
    report = _assess(
        "- **Sev1 incidents** require acknowledgment within 15 minutes.\n"
        "- **Sev2 incidents** require acknowledgment within 2 hours.\n"
        "- **Sev3 incidents** are addressed on the next business day. [S1]"
    )
    assert report.score == 1.0
    assert (report.claims, report.supported, report.citations) == (3, 3, 1)


def test_two_markers_in_one_block_cover_it_and_both_ids_are_published() -> None:
    """Coverage is a set of claim spans, not an assignment of claims to citations — so which
    marker covers which sentence never has to be decided. Both chunks still reach
    `citation_ids`, because that is what FR-MSG-04's source line and the FR-CIT-03 cards are
    built from."""
    report = _assess(
        "Approvals are recorded in the release ticket for every release. [S1] "
        "A request that goes unanswered expires after thirty days. "
        "It must then be raised again from scratch. [S2]"
    )
    assert report.score == 1.0
    assert report.citation_ids == ("chunk-1", "chunk-2")


def test_a_marker_does_not_vouch_for_the_next_sentence() -> None:
    """The rule that makes the metric mean anything: a citation supports what precedes it, so
    one marker cannot launder an entire uncited answer."""
    report = _assess(
        "The policy requires two approvals. [S1] "
        "Requests expire after thirty days. "
        "Escalation is manual. "
        "Nothing is documented anywhere."
    )
    assert report.supported == 1
    assert report.score < 0.5


def test_a_leading_marker_attaches_forward_when_nothing_precedes_it() -> None:
    report = _assess("[S1] The policy requires two approvals before release.")
    assert report.score == 1.0


def test_a_marker_on_an_uncited_heading_does_not_vouch_for_the_paragraph_below() -> None:
    """The tightening on "attach forward": a heading is not a claim, so it has no span of its
    own — and letting its marker fall through to the next paragraph would let one citation on
    a two-word title carry an entire uncited answer."""
    report = _assess("# Overview [S1]\nThe policy requires two approvals before any release.")
    assert (report.claims, report.supported) == (1, 0)
    assert report.score == 0.0


def test_a_trailing_marker_attaches_to_the_last_claim() -> None:
    report = _assess("The policy requires two approvals before any release goes out.\n\n[S1]")
    assert report.score == 1.0


# --- sentence splitting -------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Approvals are needed, e.g. from the release owner, before shipping. [S1]",
        "Approvals are needed from Dr. Chen before any release ships. [S1]",
        "Coverage rose to 3.5% across the reporting period last year. [S1]",
        "The rule is defined in v1.2 of the release policy document. [S1]",
        "Approvals come from J. Chen and the release owner on duty. [S1]",
        "Two approvals are required, i.e. one per team, before release. [S1]",
    ],
)
def test_abbreviations_and_decimals_do_not_end_a_sentence(answer: str) -> None:
    """Biased toward *not* splitting, deliberately. A false boundary invents an uncited claim
    out of half a sentence and fails a healthy answer; a missed one merges two spans, which
    errs toward passing — and R-44's "a control that refuses real questions is worse than no
    control" governs the metric as much as the threshold."""
    report = _assess(answer)
    assert report.claims == 1
    assert report.score == 1.0


def test_real_sentence_boundaries_are_still_found() -> None:
    """The anti-vacuity half of the test above: a veto list that swallowed every boundary would
    make the parametrised cases pass while measuring nothing."""
    report = _assess(
        "The policy requires two approvals! Requests expire after thirty days? "
        "Escalation is manual and undocumented."
    )
    assert report.claims == 3


def test_markdown_lines_are_separate_claims() -> None:
    report = _assess(
        "- Requests expire after thirty days. [S1]\n"
        "- Escalation is manual and undocumented. [S2]\n"
        "- Nothing is reviewed after the fact."
    )
    assert (report.claims, report.supported) == (3, 2)


def test_fenced_code_blocks_are_not_claims() -> None:
    """A code block is not a statement wanting a citation, and counting it as one would fail
    every answer that shows a configuration snippet."""
    report = _assess(
        "The retention window is set in configuration. [S1]\n"
        "```yaml\n"
        "retention_days: 30\n"
        "purge_on_expiry: true\n"
        "```\n"
    )
    assert report.claims == 1
    assert report.score == 1.0


# --- the substantive-claim filter ---------------------------------------------


def test_connective_tissue_is_not_a_claim() -> None:
    report = _assess("The policy requires two approvals before release. [S1]\n\nIn summary:")
    assert report.claims == 1
    assert report.score == 1.0


def test_headings_and_bullets_are_stripped_before_counting_words() -> None:
    for line in ("## Overview", "- Overview", "1. Overview", "> Overview"):
        report = _assess(f"{line}\nApprovals are required for every release. [S1]")
        assert report.claims == 1, line
        assert report.score == 1.0, line


def test_the_claim_floor_is_configurable_and_load_bearing() -> None:
    """Anti-vacuity: with the floor at 1 the connective line *does* count, which proves the
    filter is what excluded it rather than the sentence splitter losing it."""
    answer = "The policy requires two approvals before release. [S1]\n\nIn summary:"
    assert _assess(answer, min_claim_words=1).claims == 2
    assert _assess(answer, min_claim_words=1).score == 0.5


# --- never raises -------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    ["", "   ", "\n\n\n", "[S1]", "[S1][S2][S3]", "...", "###", "- ", "```\n", "|a|b|"],
)
def test_assess_never_raises_and_stays_in_range(answer: str) -> None:
    report = _assess(answer)
    assert 0.0 <= report.score <= 1.0
    assert report.supported <= report.claims


def test_assess_handles_a_pathologically_long_answer() -> None:
    """`LLM_MAX_OUTPUT_TOKENS` bounds this in production, but the never-raises contract is in
    the signature, so it has to hold for whatever a checkpoint hands back."""
    report = _assess("The policy requires two approvals. [S1] " * 5_000)
    assert 0.0 <= report.score <= 1.0


def test_an_answer_with_no_claims_but_a_citation_is_not_treated_as_fabrication() -> None:
    """`claims == 0` is a one-line answer or a bare heading. Citing something is the honest
    signal; citing nothing scores 0.0 exactly as an uncited paragraph does."""
    assert _assess("Yes. [S1]").score == 1.0
    assert _assess("Yes.").score == 0.0


def test_assess_with_no_sources_resolves_nothing() -> None:
    """A turn whose grounding set vanished: every marker is out of range, so it is dropped and
    the answer scores as uncited rather than as citing something that was never supplied."""
    report = _assess("The policy requires two approvals. [S1]", ids=[])
    assert (report.citations, report.markers_dropped) == (0, 1)
    assert report.score == 0.0
    assert report.reason == "no_citations"


# --- the retry probe ----------------------------------------------------------


def test_the_probe_is_the_answer_with_its_markers_removed() -> None:
    segments, _ = split_answer_segments(
        "The policy requires two approvals. [S1] Requests expire after thirty days. [S2]", IDS
    )
    probe = hypothetical_probe(segments, settings=_settings())
    assert probe is not None
    assert "[S1]" not in probe and "[S2]" not in probe
    assert probe.startswith("The policy requires two approvals.")
    assert "  " not in probe  # whitespace a removed marker doubled is collapsed


def test_a_short_answer_is_not_worth_a_probe() -> None:
    segments, _ = split_answer_segments("No. [S1]", IDS)
    assert hypothetical_probe(segments, settings=_settings()) is None


def test_the_probe_is_truncated_to_the_router_cap() -> None:
    """`sub_queries` is checkpointed state, so the probe is bounded at the source rather than
    relying on `build_probes` to cap it on read."""
    settings = Settings(gate=GateSettings(), router=RouterSettings(max_probe_chars=60))
    segments, _ = split_answer_segments("word " * 500, IDS)
    probe = hypothetical_probe(segments, settings=settings)
    assert probe is not None
    assert len(probe) <= 60


def test_the_probe_floor_is_configurable() -> None:
    segments, _ = split_answer_segments("Two approvals are required.", IDS)
    assert hypothetical_probe(segments, settings=_settings(min_probe_chars=1)) is not None
    assert hypothetical_probe(segments, settings=_settings(min_probe_chars=200)) is None


# --- live API (skipped without a key) -----------------------------------------
#
# The metric is a model-behaviour artefact, not an algorithm: it measures how a real model
# places its markers, and no double can vouch for that. The first shipped rule scored healthy
# `gpt-4o` answers **0.25–0.5** and would have abstained on 2 of 12 grounded answers — a defect
# found here and nowhere else, exactly like R-45's two inert router branches.


def _live_chat():  # noqa: ANN202
    from app.config import get_settings
    from app.services.llm import OpenAIChatClient

    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live groundedness tests skipped")
    return OpenAIChatClient(settings), settings


#: The real `RERANK_TOP_K` grounding shape — eight chunks, one of which answers.
_LIVE_PASSAGES = [
    ("release-policy.pdf", "p. 3",
     "Every production release requires two independent approvals: one from the owning team's "
     "tech lead and one from the release manager on duty. Approvals are recorded in the release "
     "ticket and cannot be granted by the same person twice."),
    ("release-policy.pdf", "p. 4",
     "An approval request that receives no response expires after 30 days. Expired requests "
     "must be raised again from scratch; there is no mechanism to extend one."),
    ("escalation.docx", "Operations > Escalation > Timing",
     "Sev1 incidents must be acknowledged within 15 minutes. Sev2 within 2 hours. Sev3 is "
     "handled on the next business day."),
    ("retention.md", "Data > Retention",
     "Audit records are retained for seven years. Application logs are retained for 90 days "
     "and are purged automatically; there is no manual purge path."),
]  # fmt: skip


def _live_sources() -> list:
    from app.rag.prompts import PromptSource

    return [
        PromptSource(chunk_id=f"c{i}", filename=name, text=text, locator=loc)
        for i, (name, loc, text) in enumerate(_LIVE_PASSAGES)
    ]


@pytest.mark.parametrize(
    "question",
    [
        "How many approvals does a production release need?",
        "What happens if an approval request is ignored?",
        "Compare the acknowledgement times across the severity tiers.",
        "Summarise the retention rules for logs and audit records.",
        "Can the same person approve a release twice?",
    ],
)
async def test_live_a_grounded_answer_clears_the_threshold(question: str) -> None:
    """The measurement that settles `GATE_MIN_GROUNDEDNESS`, and the one no mock can make.

    The last three questions are the shapes that broke the first rule: a multi-sentence
    paragraph, a bulleted comparison and a two-part summary, each of which a real model cites
    **once at the end** rather than per sentence. A gate that abstains on these is the
    false-positive failure R-44 calls worse than no control.
    """
    from app.rag.generation import generate_answer

    chat, settings = _live_chat()
    try:
        result = await generate_answer(
            query=question, sources=_live_sources(), chat=chat, settings=settings
        )
    finally:
        await chat.aclose()

    segments, dropped = split_answer_segments(result.text, result.source_ids)
    report = assess(segments, markers_dropped=dropped, settings=settings)
    assert report.score >= settings.gate.min_groundedness, (
        f"a grounded answer scored {report.score} "
        f"({report.supported}/{report.claims} claims): {result.text!r}"
    )
    assert dropped == 0


async def test_live_an_ungroundable_question_scores_zero_and_fires_the_gate() -> None:
    """The other side, and the reason the structural metric is defensible at all: a model
    answering from pre-training has **nothing to cite**, so the fabrication signature is
    observable without reading the passages semantically."""
    from app.rag.generation import generate_answer

    chat, settings = _live_chat()
    try:
        result = await generate_answer(
            query="What is our parental leave policy?",
            sources=_live_sources(),
            chat=chat,
            settings=settings,
        )
    finally:
        await chat.aclose()

    segments, dropped = split_answer_segments(result.text, result.source_ids)
    report = assess(segments, markers_dropped=dropped, settings=settings)
    assert report.score == 0.0, result.text
    assert report.reason == "no_citations"
    assert report.score < settings.gate.min_groundedness


# --- guards -------------------------------------------------------------------


def test_telemetry_names_are_the_rag_gate_vocabulary() -> None:
    """Outside the closed `graph.turn.*` set, which is a span-pairing contract (R-43(5)), and
    carrying no payload text — the R-45(8) / R-47(6) precedent."""
    from app.rag import telemetry

    names = {
        groundedness_module.GATE_COMPLETED,
        groundedness_module.GATE_RETRY,
        groundedness_module.GATE_ABSTAINED,
        groundedness_module.GATE_ADAPTED,
    }
    assert all(name.startswith("rag.gate.") for name in names)
    assert not names & telemetry.EVENT_NAMES


def test_groundedness_module_imports_no_langgraph() -> None:
    """`app.rag.graph` calls `apply_strict_msgpack()` at import time; T-402's persist step and
    T-309's evaluation job need this module without that side effect."""
    text = Path(groundedness_module.__file__).read_text(encoding="utf-8")
    for needle in ("langgraph", "langchain"):
        assert f"import {needle}" not in text and f"{needle} import" not in text, (
            f"app/rag/groundedness.py imports `{needle}` — it must stay reachable without "
            "triggering `apply_strict_msgpack()`"
        )
