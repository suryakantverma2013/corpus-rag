"""Aggregating and rendering a golden-set run (T-312).

Two rules shape everything here.

**Never average across bands.** One band is *designed* to score zero — an `unanswerable`
question should produce a decline, and a decline is correctly unfaithful to a context that does
not support it. A single overall mean would therefore move with the band mix rather than with
system quality, which is the fastest way to make a number that looks authoritative and means
nothing.

**Report what is missing as missing.** Every metric here is optional: a judge call can fail
(R-50(3) fails open, per metric), the reference-based pair runs on one band only, and recall@k
is undefined where no supporting passage was authored. Absent is rendered `—` and excluded from
the mean, never coerced to 0.0 — the FR-EVL-02 rule for chips, applied to the aggregate.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

__all__ = ["ItemScore", "RunReport", "aggregate", "render_text"]

#: The cut used to call a faithfulness score "low" when counting gate disagreements. A
#: **reporting** threshold, not a product one: nothing serves or refuses on it, and R-49(4)'s
#: `GATE_MIN_GROUNDEDNESS` governs the gate itself. Chosen to match it so the two tables are
#: read on the same scale.
FAITHFULNESS_LOW = 0.5  # TBD(§8.4)

_BAND_ORDER = ("answerable", "unanswerable", "near_miss")
_METRICS = (
    "relevancy",
    "faithfulness",
    "ctx_precision",
    "ctx_recall",
    "groundedness",
    "recall_at_k",
)


@dataclass(frozen=True, slots=True)
class ItemScore:
    """Everything one golden item produced. The JSON artifact is a list of these."""

    question_id: str
    band: str
    question: str
    answer: str = ""
    relevancy: float | None = None
    faithfulness: float | None = None
    ctx_precision: float | None = None
    ctx_recall: float | None = None
    groundedness: float | None = None
    recall_at_k: float | None = None
    gate_verdict: str | None = None
    query_class: str | None = None
    supporting_passage_ids: tuple[str, ...] = ()
    grounding_passage_ids: tuple[str, ...] = ()
    cited_passage_ids: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def mean_present(values: Iterable[float | None]) -> float | None:
    """Mean over the values that exist, or ``None`` when none do."""
    present = [v for v in values if v is not None]
    return statistics.fmean(present) if present else None


@dataclass(frozen=True, slots=True)
class RunReport:
    """The aggregate. `by_band` is the headline; `overall` exists for the metrics it suits."""

    items: tuple[ItemScore, ...]
    by_band: dict[str, dict[str, float | None]]
    counts: dict[str, int]
    disagreements: dict[str, object]
    meta: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "meta": self.meta,
            "counts": self.counts,
            "by_band": self.by_band,
            "disagreements": self.disagreements,
            "items": [asdict(item) for item in self.items],
        }


def _gate_faithfulness_table(items: Sequence[ItemScore]) -> dict[str, object]:
    """The R-49 revisit evidence: where the structural gate and the semantic judge differ.

    Two counts carry the argument, and they are not symmetric.

    ``gate_passed_low_faithfulness`` is what a pre-serve judge **would have caught** — an
    answer that cited its sources correctly enough to satisfy FR-CIT-06(4) structurally while
    the sources do not actually support it. That is OI-34's scenario and the limitation
    R-49(1) named in prose.

    ``gate_abstained_high_faithfulness`` is what a pre-serve judge **would have cost**: the
    gate refused an answer the judge considers grounded. Both numbers are needed, because a
    control that catches a rare failure by refusing common good answers is not an improvement.
    """
    pairs = [
        (item, item.faithfulness)
        for item in items
        if item.ok and item.gate_verdict is not None and item.faithfulness is not None
    ]
    caught = [i.question_id for i, f in pairs if i.gate_verdict == "pass" and f < FAITHFULNESS_LOW]
    cost = [
        i.question_id
        for i, f in pairs
        if i.gate_verdict in ("abstain", "retry") and f >= FAITHFULNESS_LOW
    ]
    return {
        "comparable_items": len(pairs),
        "faithfulness_low_cut": FAITHFULNESS_LOW,
        "gate_passed_low_faithfulness": caught,
        "gate_abstained_high_faithfulness": cost,
        "agreement": len(pairs) - len(caught) - len(cost),
    }


def aggregate(items: Sequence[ItemScore], *, meta: dict[str, object] | None = None) -> RunReport:
    by_band: dict[str, dict[str, float | None]] = {}
    for band in _BAND_ORDER:
        band_items = [i for i in items if i.band == band and i.ok]
        if not band_items:
            continue
        by_band[band] = {
            metric: mean_present(getattr(i, metric) for i in band_items) for metric in _METRICS
        }

    counts = {
        "items": len(items),
        "ok": sum(1 for i in items if i.ok),
        "errored": sum(1 for i in items if not i.ok),
        **{f"band_{band}": sum(1 for i in items if i.band == band) for band in _BAND_ORDER},
    }
    return RunReport(
        items=tuple(items),
        by_band=by_band,
        counts=counts,
        disagreements=_gate_faithfulness_table(items),
        meta=dict(meta or {}),
    )


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_text(report: RunReport) -> str:
    """The stdout view. Wide enough to read, narrow enough to paste into a ruling."""
    lines: list[str] = []
    meta = report.meta
    lines.append("=" * 96)
    lines.append("T-312 golden-set run")
    if meta:
        for key in sorted(meta):
            lines.append(f"  {key}: {meta[key]}")
    counts = report.counts
    lines.append(f"  items: {counts['items']}  ok: {counts['ok']}  errored: {counts['errored']}")
    lines.append("=" * 96)

    lines.append("")
    lines.append("Per band (mean over the items where the metric exists)")
    header = (
        f"{'band':<14}{'relevancy':>11}{'faithful':>11}{'ctx_prec':>11}"
        f"{'ctx_recall':>12}{'grounded':>11}{'recall@k':>11}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for band, metrics in report.by_band.items():
        lines.append(
            f"{band:<14}{_fmt(metrics['relevancy']):>11}{_fmt(metrics['faithfulness']):>11}"
            f"{_fmt(metrics['ctx_precision']):>11}{_fmt(metrics['ctx_recall']):>12}"
            f"{_fmt(metrics['groundedness']):>11}{_fmt(metrics['recall_at_k']):>11}"
        )

    lines.append("")
    lines.append("Gate (structural, pre-serve) vs Faithfulness (semantic, post-hoc) — R-49 revisit")
    dis = report.disagreements
    lines.append(f"  comparable items:                 {dis['comparable_items']}")
    lines.append(f"  agreed:                           {dis['agreement']}")
    lines.append(
        f"  gate passed, faithfulness < {dis['faithfulness_low_cut']}:   "
        f"{len(dis['gate_passed_low_faithfulness'])}  "  # type: ignore[arg-type]
        f"{dis['gate_passed_low_faithfulness'] or ''}"
    )
    lines.append(
        f"  gate abstained, faithfulness >= {dis['faithfulness_low_cut']}: "
        f"{len(dis['gate_abstained_high_faithfulness'])}  "  # type: ignore[arg-type]
        f"{dis['gate_abstained_high_faithfulness'] or ''}"
    )

    lines.append("")
    lines.append("Per item")
    item_header = (
        f"{'id':<7}{'band':<13}{'rel':>6}{'faith':>7}{'prec':>6}{'rec':>6}"
        f"{'grnd':>6}{'r@k':>6}  {'gate':<9}cited"
    )
    lines.append(item_header)
    lines.append("-" * len(item_header))
    for item in report.items:
        if not item.ok:
            lines.append(f"{item.question_id:<7}{item.band:<13}  ERROR: {item.error}")
            continue
        lines.append(
            f"{item.question_id:<7}{item.band:<13}{_fmt(item.relevancy):>6}"
            f"{_fmt(item.faithfulness):>7}{_fmt(item.ctx_precision):>6}"
            f"{_fmt(item.ctx_recall):>6}{_fmt(item.groundedness):>6}"
            f"{_fmt(item.recall_at_k):>6}  {(item.gate_verdict or '—'):<9}"
            f"{', '.join(item.cited_passage_ids) or '—'}"
        )
    lines.append("")
    return "\n".join(lines)
