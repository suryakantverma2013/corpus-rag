"""Feedback-driven calibration — what accumulated 👍/👎 actually says (OI-24, T-609, R-80).

Run it::

    cd backend && uv run python -m tools.feedback_calibration
    cd backend && uv run python -m tools.feedback_calibration --days 30 --json

**What this is, and what it deliberately is not.** OI-24 has been open since Rev 0.5.1: the
FR-MSG-08 thumb is collected and stored and *nothing consumes it*. R-80 closes it by ruling
that feedback is **a measurement, not a controller** — this report produces the evidence for
tuning `EVAL_ESCALATE_BELOW` and `GATE_MIN_GROUNDEDNESS`, and a human makes the change.

There is deliberately **no automatic loop**, and the reasons are in R-80(2). The short form:
a thumb is a single bit over a confounded event — it can mean the answer was wrong, the
retrieval missed, the model was verbose, the user disagreed with their own document, or they
mis-clicked — so it does not isolate the subsystem a knob belongs to; its *direction* is
ambiguous (a 👎 on a served answer argues for a stricter gate, a 👎 on an abstention for a
looser one); and the knobs in question are **safety controls**. `GATE_MIN_GROUNDEDNESS` is
what stops ungrounded text reaching a user (FR-CIT-06), so a loop that let an aggregate of
thumbs lower it would let users switch off grounding by disliking answers.

**The highest-value thing feedback can do is judge the judge.** Every other score in Corpus
is a model assessing a model — DeepEval's relevancy and faithfulness (R-50), the rerank
score (R-47), the gate's structural coverage (R-49). The thumb is the only *human*
judgement in the system, and R-70 already ruled the DeepEval numerals *indicative, not
exact* on measured evidence (two frontier judges disagreeing by ≥0.25 on 22% of chips).
So the first section below is agreement: **do the automated scores separate the answers
users liked from the ones they did not?** If they do not, no threshold on them is worth
tuning, and that is a finding no amount of swapping judge models produces.

**The sample floor is the honesty control.** Below `--min-sample` the report prints coverage
and refuses to draw conclusions. Feedback is sparse by nature — most users never rate — and
a mean over eight ratings moves more than five points when one person changes their mind.
A calibration instrument that produced confident-looking tables from n=3 would be worse than
none, which is §8.64(2)'s argument in a second place.

Reads only. No writes, no schedule, no API surface: no FR-ANL card reads this (R-43(5)'s
card-by-card check still holds), so putting it behind a route would add DTOs, authorization
and a generated client for a report an operator runs. The `tools.spec_xref` precedent
(T-607) — *the objective rule is a test, the judgement half is a report*.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.enums import Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.turn_telemetry import TurnTelemetry
from app.db.session import get_sessionmaker
from app.rag.citations import SOURCE_IDS_KEY

__all__ = [
    "DEFAULT_MIN_SAMPLE",
    "CalibrationReport",
    "RatedTurn",
    "ThresholdRow",
    "build_report",
    "main",
    "sweep",
]

#: Below this many rated answers the report refuses to conclude anything. Judgement, not
#: measurement: with fewer, a single rating moves a group mean by more than five points and
#: the sweep's counts are all single digits, so every table would invite a decision the data
#: cannot support. Overridable per run with `--min-sample`; revisit once a deployment has
#: enough accumulated feedback to measure what a stable estimate actually costs.
DEFAULT_MIN_SAMPLE = 20  # TBD(§8.4)

#: The candidate thresholds the sweep reports. Spans both live knobs: `GATE_MIN_GROUNDEDNESS`
#: ships at 0.5 (R-49(4)) and `EVAL_ESCALATE_BELOW` at 0.9 (R-53), so a useful sweep has to
#: cover the whole range rather than a neighbourhood of either.
SWEEP_POINTS: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass(frozen=True, slots=True)
class RatedTurn:
    """One answer a human rated, with every automated score recorded about it.

    `outcome` and `groundedness` come from `turn_telemetry` (T-604/T-609) and the rest from
    `messages`. The join is on `message_id`, which is exactly why R-79(1) put it there: the
    two stores are joinable without either owning the other.
    """

    message_id: str
    thumb: Feedback
    relevancy: float | None
    faithfulness: float | None
    groundedness: float | None
    outcome: str | None
    citation_count: int
    model_name: str | None

    @property
    def liked(self) -> bool:
        return self.thumb is Feedback.UP


@dataclass(frozen=True, slots=True)
class ThresholdRow:
    """What one candidate threshold would have done to the population users rated.

    Framed as *withholding*, because that is what both knobs do — `GATE_MIN_GROUNDEDNESS`
    abstains rather than serving, `EVAL_ESCALATE_BELOW` sends the answer for a second
    opinion. `caught` is the win (a disliked answer scoring below the line); `false_alarms`
    is the cost (a liked one). A threshold is worth moving only when the first grows faster
    than the second, and this table is the only way to see that.
    """

    threshold: float
    caught: int
    missed: int
    false_alarms: int
    passed: int

    @property
    def precision(self) -> float | None:
        """Of the answers this threshold would withhold, how many did users dislike?"""
        flagged = self.caught + self.false_alarms
        return self.caught / flagged if flagged else None

    @property
    def recall(self) -> float | None:
        """Of the answers users disliked, how many would this threshold have withheld?"""
        disliked = self.caught + self.missed
        return self.caught / disliked if disliked else None


@dataclass(slots=True)
class CalibrationReport:
    """The whole answer, including the case where the answer is 'not yet'."""

    window_days: int
    min_sample: int
    turns_total: int
    answers_total: int
    rated: int
    liked: int
    disliked: int
    conclusive: bool = False
    separation: dict[str, dict[str, float | None]] = field(default_factory=dict)
    sweeps: dict[str, list[ThresholdRow]] = field(default_factory=dict)
    citation_counts: dict[str, float | None] = field(default_factory=dict)
    outcome_mix: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def coverage(self) -> float | None:
        """Share of answers carrying a rating. The denominator of everything below."""
        return self.rated / self.answers_total if self.answers_total else None


def _rated_query(since: datetime, owner_id: uuid.UUID | None) -> Select[Any]:
    """AI answers carrying a thumb, with their telemetry row when one exists.

    An **outer** join: `turn_telemetry` starts empty on an existing deployment and only fills
    from the first turn after T-604, so an inner join would silently report zero rated turns
    on exactly the corpus that has the most accumulated feedback. `outcome` and `groundedness`
    are then simply absent for older rows, which the report distinguishes from zero.
    """
    stmt = (
        select(Message, TurnTelemetry)
        .outerjoin(TurnTelemetry, TurnTelemetry.message_id == Message.id)
        .where(
            Message.role == MessageRole.AI,
            Message.feedback.is_not(None),
            Message.created_at >= since,
        )
        .order_by(Message.created_at)
    )
    if owner_id is not None:
        stmt = stmt.join(Conversation, Conversation.id == Message.conversation_id).where(
            Conversation.owner_id == owner_id
        )
    return stmt


def _citation_count(citations: dict | None) -> int:
    """How many passages grounded this answer (R-50(5)'s `source_ids` envelope)."""
    if not citations:
        return 0
    sources = citations.get(SOURCE_IDS_KEY)
    return len(sources) if isinstance(sources, list) else 0


def _score(evaluation: dict | None, key: str) -> float | None:
    if not evaluation:
        return None
    value = evaluation.get(key)
    return float(value) if isinstance(value, int | float) else None


def sweep(turns: Sequence[RatedTurn], *, metric: str) -> list[ThresholdRow]:
    """What each candidate threshold on `metric` would have done to the rated population.

    Turns with no score for the metric are **excluded rather than counted as passing**. A
    missing score means the judge failed open (R-50(3)) or the turn never reached the gate —
    not that the answer scored well — and folding those into `passed` would make every
    threshold look better than it is, most of all the strict ones.
    """
    scored = [(turn, getattr(turn, metric)) for turn in turns]
    scored = [(turn, value) for turn, value in scored if value is not None]
    if not scored:
        # No rows rather than ten rows of zeros. A table of zeros is not a result, and in the
        # `--json` output it is indistinguishable from a threshold that genuinely flagged
        # nothing — which is the reading that would send someone tuning on an empty metric.
        return []

    rows: list[ThresholdRow] = []
    for threshold in SWEEP_POINTS:
        caught = missed = false_alarms = passed = 0
        for turn, value in scored:
            below = value < threshold
            if turn.liked:
                false_alarms += below
                passed += not below
            else:
                caught += below
                missed += not below
        rows.append(
            ThresholdRow(
                threshold=threshold,
                caught=caught,
                missed=missed,
                false_alarms=false_alarms,
                passed=passed,
            )
        )
    return rows


def _separation(turns: Sequence[RatedTurn], metric: str) -> dict[str, float | None]:
    """Median score for liked vs disliked answers, and the gap between them.

    **The gap is the number that matters**, and it is reported as a median rather than a mean
    on T-313's rule — a live assertion about model behaviour is a distribution, not a value,
    and one 0.0 from a judge that failed open would drag a mean of eight through the floor.
    A gap at or below zero means the metric does not see what the users saw, and no threshold
    on it is worth tuning.
    """
    liked = [getattr(t, metric) for t in turns if t.liked and getattr(t, metric) is not None]
    disliked = [getattr(t, metric) for t in turns if not t.liked and getattr(t, metric) is not None]
    liked_median = statistics.median(liked) if liked else None
    disliked_median = statistics.median(disliked) if disliked else None
    gap = (
        liked_median - disliked_median
        if liked_median is not None and disliked_median is not None
        else None
    )
    return {
        "liked_n": float(len(liked)),
        "disliked_n": float(len(disliked)),
        "liked_median": liked_median,
        "disliked_median": disliked_median,
        "gap": gap,
    }


async def build_report(
    session: AsyncSession,
    *,
    window_days: int,
    min_sample: int,
    owner_id: uuid.UUID | None = None,
) -> CalibrationReport:
    """Gather everything the thumbs can support, and mark whether they support it.

    ``owner_id`` narrows every count to one user. That is a real operator question rather
    than a convenience: OI-15 makes knowledge-base scope **global-per-user**, so two users'
    corpora are disjoint and a threshold that suits one may not suit the other — and a
    deployment where a single heavy user supplies most of the feedback would otherwise have
    its thresholds calibrated by that user alone without anyone being able to see it.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)

    answers_stmt = select(Message.id).where(
        Message.role == MessageRole.AI, Message.created_at >= since
    )
    turns_stmt = select(TurnTelemetry.id).where(TurnTelemetry.created_at >= since)
    if owner_id is not None:
        answers_stmt = answers_stmt.join(
            Conversation, Conversation.id == Message.conversation_id
        ).where(Conversation.owner_id == owner_id)
        turns_stmt = turns_stmt.where(TurnTelemetry.owner_id == owner_id)

    answers_total = len((await session.scalars(answers_stmt)).all())
    turns_total = len((await session.scalars(turns_stmt)).all())

    turns: list[RatedTurn] = []
    for message, telemetry in (await session.execute(_rated_query(since, owner_id))).all():
        turns.append(
            RatedTurn(
                message_id=str(message.id),
                thumb=message.feedback,
                relevancy=_score(message.evaluation, "relevancy"),
                faithfulness=_score(message.evaluation, "faithfulness"),
                groundedness=telemetry.groundedness if telemetry is not None else None,
                outcome=telemetry.outcome if telemetry is not None else None,
                citation_count=_citation_count(message.citations),
                model_name=message.model_name,
            )
        )

    report = CalibrationReport(
        window_days=window_days,
        min_sample=min_sample,
        turns_total=turns_total,
        answers_total=answers_total,
        rated=len(turns),
        liked=sum(1 for t in turns if t.liked),
        disliked=sum(1 for t in turns if not t.liked),
    )

    # **The floor is checked before anything is computed, not after.** A report that filled
    # in the tables and then printed a caveat would still be read as a result — the tables are
    # what a reader acts on, and a warning above them loses to a number inside them.
    if report.rated < min_sample or not report.liked or not report.disliked:
        return report

    report.conclusive = True
    for metric in ("relevancy", "faithfulness", "groundedness"):
        report.separation[metric] = _separation(turns, metric)
        report.sweeps[metric] = sweep(turns, metric=metric)

    liked_citations = [t.citation_count for t in turns if t.liked]
    disliked_citations = [t.citation_count for t in turns if not t.liked]
    report.citation_counts = {
        "liked_median": statistics.median(liked_citations) if liked_citations else None,
        "disliked_median": statistics.median(disliked_citations) if disliked_citations else None,
    }

    for label, subset in (("liked", True), ("disliked", False)):
        mix: dict[str, int] = {}
        for turn in turns:
            if turn.liked is not subset:
                continue
            mix[turn.outcome or "unrecorded"] = mix.get(turn.outcome or "unrecorded", 0) + 1
        report.outcome_mix[label] = mix

    return report


# --- rendering ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def render(report: CalibrationReport) -> str:
    settings = get_settings()
    out: list[str] = []
    out.append(
        f"feedback calibration - last {report.window_days} days "
        f"({report.turns_total} turns, {report.answers_total} answers)"
    )
    out.append("")
    out.append(
        f"  rated: {report.rated}  (up {report.liked} / down {report.disliked})   "
        f"coverage {_pct(report.coverage)}"
    )

    if not report.conclusive:
        out.append("")
        out.append(
            f"  NOT CONCLUSIVE - needs at least {report.min_sample} rated answers with both "
            "up and down ratings present."
        )
        out.append(
            "  Feedback is sparse by nature; a mean over a handful of ratings moves further "
            "than any threshold you would set from it."
        )
        out.append("  Nothing below is reported until there is enough of it. (R-80(3))")
        return "\n".join(out)

    out.append("")
    out.append("  DOES THE JUDGE SEE WHAT THE USERS SAW?")
    out.append("  (a gap at or below zero means no threshold on that metric is worth moving)")
    out.append("")
    out.append("    metric         liked   disliked    gap")
    for metric, stats in report.separation.items():
        out.append(
            f"    {metric:<13} {_num(stats['liked_median']):>6} "
            f"{_num(stats['disliked_median']):>10} {_num(stats['gap']):>6}"
        )

    for metric, rows in report.sweeps.items():
        gap = report.separation[metric]["gap"]
        if gap is None or gap <= 0:
            continue
        out.append("")
        out.append(f"  WITHHOLDING ON {metric.upper()} - what each threshold would have done")
        out.append("    thresh   caught   missed   false alarms   precision   recall")
        for row in rows:
            out.append(
                f"    {row.threshold:>6.1f} {row.caught:>8} {row.missed:>8} "
                f"{row.false_alarms:>14} {_pct(row.precision):>11} {_pct(row.recall):>8}"
            )

    out.append("")
    out.append("  RETRIEVAL CHARACTERISTICS")
    out.append(
        f"    median citations   liked {_num(report.citation_counts['liked_median'])}   "
        f"disliked {_num(report.citation_counts['disliked_median'])}"
    )
    for label, mix in report.outcome_mix.items():
        rendered = ", ".join(f"{k} {v}" for k, v in sorted(mix.items())) or "none"
        out.append(f"    outcomes ({label}): {rendered}")

    out.append("")
    out.append("  SHIPPED THRESHOLDS, for comparison")
    out.append(f"    GATE_MIN_GROUNDEDNESS = {settings.gate.min_groundedness}")
    out.append(f"    EVAL_ESCALATE_BELOW   = {settings.eval.escalate_below}")
    out.append("")
    out.append(
        "  These are evidence, not a recommendation. R-80(2): the change is a human's, and "
        "GATE_MIN_GROUNDEDNESS is a safety control (FR-CIT-06) before it is a tuning knob - "
        "R-49(4) stands, that healthy answers clustering below it means the metric is wrong "
        "rather than the number."
    )
    return "\n".join(out)


def _as_json(report: CalibrationReport) -> str:
    return json.dumps(
        {
            "window_days": report.window_days,
            "turns_total": report.turns_total,
            "answers_total": report.answers_total,
            "rated": report.rated,
            "liked": report.liked,
            "disliked": report.disliked,
            "coverage": report.coverage,
            "conclusive": report.conclusive,
            "separation": report.separation,
            "sweeps": {
                metric: [
                    {
                        "threshold": row.threshold,
                        "caught": row.caught,
                        "missed": row.missed,
                        "false_alarms": row.false_alarms,
                        "precision": row.precision,
                        "recall": row.recall,
                    }
                    for row in rows
                ]
                for metric, rows in report.sweeps.items()
            },
            "citation_counts": report.citation_counts,
            "outcome_mix": report.outcome_mix,
        },
        indent=2,
    )


async def _run(window_days: int, min_sample: int, as_json: bool, owner_id: uuid.UUID | None) -> int:
    async with get_sessionmaker()() as session:
        report = await build_report(
            session, window_days=window_days, min_sample=min_sample, owner_id=owner_id
        )
    print(_as_json(report) if as_json else render(report))
    # Always 0. This reports; it does not gate anything, and a non-zero exit would invite
    # someone to wire it into CI, where "not enough feedback yet" would read as a failure.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.feedback_calibration",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=90, help="window to report over (default 90)")
    parser.add_argument(
        "--min-sample",
        type=int,
        default=DEFAULT_MIN_SAMPLE,
        help=f"rated answers required before conclusions are drawn (default {DEFAULT_MIN_SAMPLE})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--owner", type=uuid.UUID, default=None, help="narrow to one user's feedback"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.days, args.min_sample, args.json, args.owner))


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
