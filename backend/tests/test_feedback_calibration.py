"""Feedback-driven calibration (OI-24, T-609, R-80).

The report's job is to turn accumulated 👍/👎 into evidence for tuning `EVAL_ESCALATE_BELOW`
and `GATE_MIN_GROUNDEDNESS`. Three properties carry it, and each fails differently:

1. **It refuses to conclude from too little.** Feedback is sparse, and an instrument that
   printed confident tables from n=3 would be worse than none — §8.64(2) in a second place.
2. **The arithmetic is right**, because a threshold sweep is what someone would act on.
3. **A missing score is excluded, never counted as passing.** That is the one mistake that
   makes every threshold look better than it is, and it flatters the strict ones most.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tools.feedback_calibration import (
    DEFAULT_MIN_SAMPLE,
    RatedTurn,
    build_report,
    render,
    sweep,
)

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.turn_telemetry import TurnTelemetryRepository
from app.db.repositories.users import UserRepository
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY
from app.rag.telemetry import TurnRecord


def _turn(
    *,
    liked: bool,
    relevancy: float | None = None,
    faithfulness: float | None = None,
    groundedness: float | None = None,
    outcome: str | None = "answered",
    citations: int = 3,
) -> RatedTurn:
    return RatedTurn(
        message_id=str(uuid.uuid4()),
        thumb=Feedback.UP if liked else Feedback.DOWN,
        relevancy=relevancy,
        faithfulness=faithfulness,
        groundedness=groundedness,
        outcome=outcome,
        citation_count=citations,
        model_name="gpt-4o",
    )


# --- 1. the sample floor ------------------------------------------------------


async def _seed(
    session: AsyncSession,
    make_token: Callable[..., str],
    *,
    ratings: list[tuple[bool, float | None]],
    with_telemetry: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One user's conversation of rated answers.

    Returns `(owner_id, conversation_id)`. Every assertion scopes to the owner, because the
    suite runs against a **real** development database whose existing rated answers would
    otherwise be counted into every total — the report reads the whole corpus by design.
    """
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=sub, email=f"{sub.hex[:8]}@corpus.local", display_name="U"
    )
    conversation = Conversation(owner_id=sub, tenant_id=DEFAULT_TENANT_ID, title="chat")
    session.add(conversation)
    await session.flush()

    repo = TurnTelemetryRepository(session)
    for index, (liked, faithfulness) in enumerate(ratings):
        answer = Message(
            conversation_id=conversation.id,
            role=MessageRole.AI,
            content="an answer",
            citations={SEGMENTS_KEY: [{"text": "a"}], SOURCE_IDS_KEY: [str(uuid.uuid4())] * 2},
            evaluation=None if faithfulness is None else {"faithfulness": faithfulness},
            feedback=Feedback.UP if liked else Feedback.DOWN,
            model_name="gpt-4o",
            latency_ms=100,
        )
        session.add(answer)
        await session.flush()
        if with_telemetry:
            await repo.record(
                TurnRecord(
                    conversation_id=conversation.id,
                    owner_id=sub,
                    turn_index=index,
                    outcome="answered",
                    latency_ms=100,
                    message_id=answer.id,
                    groundedness=1.0 if liked else 0.2,
                )
            )
    await session.flush()
    return sub, conversation.id


async def test_too_little_feedback_reports_coverage_and_refuses_to_conclude(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The honesty control, and it is checked **before** anything is computed.

    A report that filled in the tables and then printed a caveat would still be read as a
    result: the tables are what a reader acts on, and a warning above them loses to a number
    inside them.
    """
    owner, _conversation = await _seed(
        session, make_token, ratings=[(True, 0.9), (False, 0.2), (True, 0.8)]
    )

    report = await build_report(
        session, window_days=3650, min_sample=DEFAULT_MIN_SAMPLE, owner_id=owner
    )

    assert report.rated == 3
    assert report.conclusive is False
    assert report.separation == {}, "no conclusions may be computed below the floor"
    assert report.sweeps == {}


async def test_ratings_of_one_kind_are_not_conclusive_however_many(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Twenty thumbs-up says nothing about a threshold.

    Separation is a *comparison*; with one class present the gap is undefined, and a sweep
    over it would report zero false alarms at every threshold — an instrument that
    recommends withholding everything.
    """
    owner, _conversation = await _seed(session, make_token, ratings=[(True, 0.9)] * 25)

    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    assert report.rated == 25
    assert report.liked == 25 and report.disliked == 0
    assert report.conclusive is False


async def test_enough_feedback_of_both_kinds_is_conclusive(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    ratings = [(True, 0.95)] * 12 + [(False, 0.25)] * 10
    owner, _conversation = await _seed(session, make_token, ratings=ratings)

    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    assert report.conclusive is True
    assert report.rated == 22
    assert report.separation["faithfulness"]["gap"] == pytest.approx(0.70)
    assert report.separation["groundedness"]["gap"] == pytest.approx(0.80)
    assert report.sweeps["faithfulness"]


async def test_coverage_is_rated_over_answers_not_over_turns(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Coverage is the denominator of everything else, so it is asserted on its own."""
    owner, conversation_id = await _seed(session, make_token, ratings=[(True, 0.9), (False, 0.1)])
    session.add(
        Message(
            conversation_id=conversation_id,
            role=MessageRole.AI,
            content="unrated",
            citations=None,
            latency_ms=10,
        )
    )
    await session.flush()

    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    assert report.answers_total == 3
    assert report.rated == 2
    assert report.coverage == pytest.approx(2 / 3)


async def test_a_rating_older_than_the_window_is_excluded(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    owner, _conversation = await _seed(session, make_token, ratings=[(True, 0.9), (False, 0.1)])

    report = await build_report(session, window_days=0, min_sample=20, owner_id=owner)

    assert report.rated == 0


# --- 2. the sweep's arithmetic ------------------------------------------------


def test_the_sweep_counts_catches_against_false_alarms() -> None:
    """`caught` is the win, `false_alarms` the cost; a threshold moves only when the first
    grows faster than the second."""
    turns = [
        _turn(liked=False, faithfulness=0.1),
        _turn(liked=False, faithfulness=0.45),
        _turn(liked=True, faithfulness=0.85),
        _turn(liked=True, faithfulness=0.95),
    ]

    rows = {row.threshold: row for row in sweep(turns, metric="faithfulness")}

    at_half = rows[0.5]
    assert (at_half.caught, at_half.missed) == (2, 0)
    assert (at_half.false_alarms, at_half.passed) == (0, 2)
    assert at_half.precision == 1.0
    assert at_half.recall == 1.0

    at_nine = rows[0.9]
    assert at_nine.caught == 2
    assert at_nine.false_alarms == 1, "0.85 was liked and would have been withheld"
    assert at_nine.precision == pytest.approx(2 / 3)


def test_a_threshold_that_flags_nothing_reports_no_precision() -> None:
    """Precision over an empty flagged set is undefined, not 0 or 1.

    Rendering it as a number would put a confident-looking `0%` beside a threshold that
    simply never fired.
    """
    rows = {
        r.threshold: r for r in sweep([_turn(liked=True, faithfulness=0.99)], metric="faithfulness")
    }
    assert rows[0.1].precision is None
    assert rows[0.1].recall is None


def test_an_unscored_answer_is_excluded_from_the_sweep_not_counted_as_passing() -> None:
    """**The mistake that flatters every threshold**, and the strict ones most.

    A missing score means the judge failed open (R-50(3)) or the turn never reached the
    gate — not that the answer scored well. Folding those into `passed` would make a
    threshold's false-alarm count look lower than it is, which is the direction that argues
    for withholding more.
    """
    turns = [
        _turn(liked=True, faithfulness=None),
        _turn(liked=False, faithfulness=None),
        _turn(liked=False, faithfulness=0.1),
    ]

    rows = {row.threshold: row for row in sweep(turns, metric="faithfulness")}

    at_half = rows[0.5]
    assert (at_half.caught, at_half.missed, at_half.false_alarms, at_half.passed) == (1, 0, 0, 0)
    assert at_half.recall == 1.0, "recall is over the *scored* disliked answers, not all of them"


# --- 3. the join, and what survives a missing telemetry row -------------------


async def test_a_rating_with_no_telemetry_row_still_counts(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """An **outer** join, and it is load-bearing on exactly the corpus that matters most.

    `turn_telemetry` starts empty and fills only from the first turn after T-604, so an inner
    join would report zero rated answers on a deployment whose whole value here is the
    feedback it accumulated *before* that. Those rows simply have no `groundedness` and no
    `outcome`, which the report keeps distinct from zero.
    """
    owner, _conversation = await _seed(
        session, make_token, ratings=[(True, 0.9), (False, 0.1)], with_telemetry=False
    )

    report = await build_report(session, window_days=3650, min_sample=2, owner_id=owner)

    assert report.rated == 2
    assert report.conclusive is True
    assert report.separation["groundedness"]["gap"] is None, (
        "no telemetry means no groundedness, not a groundedness of zero"
    )
    assert report.separation["faithfulness"]["gap"] == pytest.approx(0.80)
    assert report.sweeps["groundedness"] == [], (
        "a sweep over a metric nothing scored must be empty, not ten rows of zeros"
    )


async def test_the_gate_score_reaches_the_report_through_telemetry(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """T-609's whole reason: `GATE_MIN_GROUNDEDNESS` is calibratable only if its input is kept.

    R-49(1) keeps the gate's score out of `messages.evaluation` and off every user surface,
    and that is unchanged — this is the operator store, and before T-609 the score lived only
    as long as a log line (R-50(6)).
    """
    owner, _conversation = await _seed(
        session, make_token, ratings=[(True, 0.9)] * 11 + [(False, 0.1)] * 11
    )

    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    separation = report.separation["groundedness"]
    assert separation["liked_median"] == pytest.approx(1.0)
    assert separation["disliked_median"] == pytest.approx(0.2)
    assert report.sweeps["groundedness"], "the gate's own threshold must be sweepable"


# --- 4. rendering -------------------------------------------------------------


async def test_the_report_renders_on_a_non_utf8_stream(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """§8.31's family: output that works until it meets a stream that is not UTF-8.

    A transitive `rich` once made worker exception logs raise `UnicodeEncodeError` *inside*
    the logging call; the same shape bit this report's first draft, whose thumb emoji crashed
    on a Windows `cp1252` console. Prose keeps the emoji; **stdout is ASCII**, which is the
    same reason `logging_config` pins `JSONRenderer`.
    """
    owner, _conversation = await _seed(
        session, make_token, ratings=[(True, 0.9)] * 11 + [(False, 0.1)] * 11
    )
    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    text = render(report)
    text.encode("cp1252")  # must not raise
    text.encode("ascii")


async def test_an_inconclusive_report_renders_no_tables(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The refusal has to be visible in the output, not only in the object."""
    owner, _conversation = await _seed(session, make_token, ratings=[(True, 0.9), (False, 0.1)])
    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    text = render(report)
    assert "NOT CONCLUSIVE" in text
    assert "WITHHOLDING ON" not in text
    assert "precision" not in text and "false alarms" not in text


async def test_a_metric_that_does_not_separate_is_not_swept(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """A gap at or below zero means the metric did not see what the users saw.

    Printing a threshold table for it would invite tuning a knob on a score that carries no
    signal — the most expensive way to make a product worse while looking rigorous.
    """
    ratings = [(True, 0.5)] * 11 + [(False, 0.5)] * 11
    owner, _conversation = await _seed(session, make_token, ratings=ratings)
    report = await build_report(session, window_days=3650, min_sample=20, owner_id=owner)

    assert report.separation["faithfulness"]["gap"] == pytest.approx(0.0)
    assert "WITHHOLDING ON FAITHFULNESS" not in render(report)
