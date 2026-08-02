"""The golden set: passages, questions, and the rules that keep them consistent (T-312).

**The corpus is authored, and that is a recorded deviation (R-52).** These passages are written
by hand and seeded straight into `document_chunks`; they do not travel the T-203/T-204
ingestion path. So chunk boundaries here are *chosen* rather than *produced*, and a future
chunker change will not move these scores. That is deliberate — this harness measures retrieval
ranking and generation quality, and a metric that moves when the splitter is tuned cannot tell
you which of the two changed — but it is a limitation to name rather than hide: nothing here
exercises the parser, the splitter or the overlap rule.

**JSON, not YAML.** `pyyaml` is present in the lockfile only as a transitive dependency of
`deepeval`, and R-42(1) already refused to build on an undeclared transitive package
(`langchain_core`). `json` is stdlib and the file is machine-written far more often than it is
hand-edited.

The three bands exist for different questions and must not be averaged together:

* ``answerable`` — one or more passages answer it. The quality baseline, and **the only band
  the reference-based metrics run on**: Contextual Recall asks whether the retrieved context
  contains what the ideal answer needed, and an ideal answer of "the documents do not say"
  needs nothing, so the metric would be measuring its own vacuity.
* ``unanswerable`` — nothing supports it. The R-23 decline path.
* ``near_miss`` — a related but non-answering passage exists (``distractor_passage_ids``). This
  is the OI-34 shape: the band most likely to produce a fabrication that still cites a real,
  in-scope chunk, which is exactly what the T-308 structural gate provably cannot catch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = ["Band", "GoldenSet", "Passage", "Question", "load_golden_set"]

Band = Literal["answerable", "unanswerable", "near_miss"]

#: The bands the reference-based metrics are computed for. See the module docstring.
REFERENCE_SCORED_BANDS: frozenset[str] = frozenset({"answerable"})

_BANDS: frozenset[str] = frozenset({"answerable", "unanswerable", "near_miss"})
_DEFAULT_PATH = Path(__file__).parent / "corpus" / "golden_set.json"


@dataclass(frozen=True, slots=True)
class Passage:
    """One authored chunk. `filename` and `locator` feed the FR-CIT-03 citation payload."""

    id: str
    topic: str
    filename: str
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class Question:
    """One golden item.

    ``expected_output`` is the *ideal* answer, not a transcript of one the system produced —
    that distinction is the whole reason these two metrics are offline (R-50(1)).
    """

    id: str
    band: Band
    question: str
    expected_output: str
    supporting_passage_ids: tuple[str, ...] = ()
    distractor_passage_ids: tuple[str, ...] = ()

    @property
    def reference_scored(self) -> bool:
        return self.band in REFERENCE_SCORED_BANDS


@dataclass(frozen=True, slots=True)
class GoldenSet:
    version: int
    passages: tuple[Passage, ...]
    questions: tuple[Question, ...]
    source: Path | None = None
    topics: tuple[str, ...] = field(default_factory=tuple)

    def by_band(self, band: str) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.band == band)

    @property
    def passage_ids(self) -> frozenset[str]:
        return frozenset(p.id for p in self.passages)


class CorpusError(ValueError):
    """The golden set is inconsistent. Raised at load, never survived.

    A corpus that references a passage it does not contain would silently score retrieval
    against an id nothing can return, which reads as a retrieval failure and is a typo.
    """


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def load_golden_set(path: Path | str | None = None) -> GoldenSet:
    """Load and validate the golden set.

    Validation is not ceremony: every rule here corresponds to a way the report would lie.
    Duplicate ids silently drop items from an aggregate; an unknown `supporting_passage_ids`
    makes recall@k unreachable and looks like a retrieval regression; an answerable question
    with no support is either a mislabelled band or a missing passage, and both would drag the
    band average down for a reason that has nothing to do with the system under test.
    """
    source = Path(path) if path is not None else _DEFAULT_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))

    passages = tuple(
        Passage(
            id=str(item["id"]),
            topic=str(item.get("topic", "")),
            filename=str(item["filename"]),
            locator=str(item["locator"]),
            text=str(item["text"]),
        )
        for item in raw.get("passages", ())
    )
    _require(bool(passages), "golden set contains no passages")
    _require(
        len({p.id for p in passages}) == len(passages),
        "duplicate passage ids in the golden set",
    )
    known = {p.id for p in passages}

    questions: list[Question] = []
    for item in raw.get("questions", ()):
        band = str(item["band"])
        _require(band in _BANDS, f"unknown band {band!r} on question {item.get('id')!r}")
        supporting = tuple(str(i) for i in item.get("supporting_passage_ids", ()))
        distractors = tuple(str(i) for i in item.get("distractor_passage_ids", ()))
        unknown = (set(supporting) | set(distractors)) - known
        _require(not unknown, f"question {item.get('id')!r} references unknown passages {unknown}")
        if band == "answerable":
            _require(
                bool(supporting),
                f"answerable question {item.get('id')!r} names no supporting passage",
            )
        else:
            _require(
                not supporting,
                f"{band} question {item.get('id')!r} must name no supporting passage",
            )
        questions.append(
            Question(
                id=str(item["id"]),
                band=band,  # type: ignore[arg-type]
                question=str(item["question"]),
                expected_output=str(item["expected_output"]),
                supporting_passage_ids=supporting,
                distractor_passage_ids=distractors,
            )
        )

    _require(bool(questions), "golden set contains no questions")
    _require(
        len({q.id for q in questions}) == len(questions),
        "duplicate question ids in the golden set",
    )

    return GoldenSet(
        version=int(raw.get("version", 1)),
        passages=passages,
        questions=tuple(questions),
        source=source,
        topics=tuple(str(t) for t in raw.get("topics", ())),
    )


def select(
    golden: GoldenSet, *, bands: Sequence[str] | None = None, limit: int | None = None
) -> tuple[Question, ...]:
    """The items one run will execute.

    ``limit`` takes from each band in turn rather than from the head of the list, so a
    `--limit 3` smoke run exercises all three bands instead of three answerable questions —
    the cheap run is the one most likely to be the only one somebody does.
    """
    wanted = tuple(bands) if bands else ("answerable", "unanswerable", "near_miss")
    pools = [list(golden.by_band(band)) for band in wanted]
    ordered: list[Question] = []
    while any(pools):
        for pool in pools:
            if pool:
                ordered.append(pool.pop(0))
    return tuple(ordered[:limit]) if limit else tuple(ordered)
