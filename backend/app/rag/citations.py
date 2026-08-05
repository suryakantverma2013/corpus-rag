"""The FR-MSG-06 citation envelope — what `messages.citations` holds (T-402).

FR-MSG-06 fixes the wire shape as ``segs: [{text} | {isCite, doc, page, quote, chunkId}]`` and
R-48(4) makes it **derived, never stored as such**: `messages.content` keeps the answer with
its ``[S<n>]`` markers intact, and this module resolves those markers into the payload the
FR-CIT-01 chip and the FR-CIT-03 hover card render.

Four things this module is careful about, each of them a ruling rather than a preference:

1. **It does not parse the answer.** :func:`build_citations` takes segments the caller already
   produced with `split_answer_segments` — the *same* parser the T-308 gate ran (R-48(4)).
   Two parsers is how the chip a user hovers stops matching the passage the gate approved.

2. **A citation whose chunk is gone is dropped, not rendered.** FR-CIT-06 says citations
   failing (1)/(3)/(5) are "dropped before display", and this is where that happens: the
   caller re-reads the cited chunks through the caller's *live* FR-RET-04 scope a superstep
   after the gate, so a passage deleted or revoked in between is simply absent from `hits` and
   its segment never reaches the envelope. R-49(3) leans on this read existing — the gate does
   no database work precisely *because* this one runs later.

3. **The FR-CIT-04 score may be absent, and is then omitted entirely** (R-47(2)). The
   reranker fails open, leaving `rerank_scores` empty, and the R-46(3) cross-probe RRF score
   must never be substituted for it — it is comparable within a turn only. A `0.0` would be a
   number a user reads as "irrelevant"; a missing key is what FR-CIT-04 already requires
   clients to handle.

4. **`source_ids` rides along.** The ordered grounding set is not recoverable from the
   segments — an answer citing `[S2]` and `[S5]` leaves positions 1, 3 and 4 unrepresented —
   and `workers/evaluate.py` needs it to replay the split. R-49(a) forbids a second parser as
   the workaround, so the list is persisted (R-50(5)).

**langgraph-free**, like `errors.py` / `generation.py` / `groundedness.py` / `budget.py`:
`app.rag.graph` calls ``apply_strict_msgpack()`` at import, and the API DTO, T-403 and T-404
all read this shape without wanting that.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    ValidationError,
    model_serializer,
)

from app.rag.generation import AnswerSegment
from app.rag.retrieval import RetrievedChunk

__all__ = [
    "SEGMENTS_KEY",
    "SOURCE_IDS_KEY",
    "CitationEnvelope",
    "CitationLocator",
    "CitationLocatorKind",
    "CitationSegment",
    "Segment",
    "TextSegment",
    "build_citations",
    "envelope_segments",
    "plain_segments",
    "scores_by_chunk_id",
]

#: The two envelope keys. Named constants because `workers/evaluate.py` reads them and a
#: silent rename there degrades to "this message is never evaluated" rather than to an error.
SEGMENTS_KEY = "segments"
SOURCE_IDS_KEY = "source_ids"

#: One envelope: ``{"segments": [...], "source_ids": [...]}``.
type CitationEnvelope = dict[str, Any]


# --- the FR-MSG-06 segment shapes (T-405, R-57) ---------------------------------------
#
# Declared here rather than in the API layer for the reason this whole module exists: these are
# the shapes *this* module writes, so `_citation` constructs the model and drift between the
# persisted payload and the published type is not merely tested for — it is unrepresentable.
# `app/api/messages.py` reads them back through `envelope_segments`.
#
# They are also what makes the citation chip a *generated* TypeScript type. `segs` was
# `list[dict[str, Any]]`, which reaches a client as `Record<string, never>[]`, so T-505 would
# have had to hand-write the one shape the spec is most explicit about — which the frontend-dev
# skill forbids outright.
#
# Field naming keeps `app/api/messages.py`'s deliberate seam: route fields are snake_case,
# **segment** keys are camelCase, because FR-MSG-06 fixes that shape and it is the persisted
# contract `workers/evaluate.py` reads.

#: R-34's three locator kinds, restated rather than imported: `LocatorKind` lives in
#: `app/ingestion/parsers/base.py`, and importing it pulls PyMuPDF, python-docx and markdown-it
#: (451 modules) into every consumer of this module — including the API process, which
#: `tests/test_parsers.py` asserts against outright. Guarded by a drift test instead.
type CitationLocatorKind = Literal["page", "section", "rows"]


class TextSegment(BaseModel):
    """A plain run of the answer.

    The persisted shape is exactly ``{"text": ...}`` and nothing may be added: this is also what
    `plain_segments` writes for abstentions, blocked turns and any pre-T-402 row.
    """

    text: str


class CitationLocator(BaseModel):
    """R-34's structured address, beside the rendered label.

    Every field is optional because `Locator.as_metadata()` drops the ones that do not apply to
    the kind — a PDF citation carries `page`, a DOCX one `section_path`, a CSV one
    `row_start`/`row_end`. FR-CIT-04 is explicit that clients read **these fields** and never
    parse the label, which is why the structured copy is published at all.
    """

    kind: CitationLocatorKind | None = None
    label: str | None = None
    page: int | None = None
    section_path: list[str] | None = None
    section_index: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class CitationSegment(BaseModel):
    """A citation run — the FR-CIT-01 chip and the FR-CIT-03 hover card.

    See :func:`_citation` for what each field carries and why `page` holds a label rather than a
    number.
    """

    isCite: Literal[True] = True  # noqa: N815 — FR-MSG-06 fixes the spelling
    doc: str
    #: Both default to `None` so an **absent** key validates, not just a null one. `_citation`
    #: always writes them, but this model also *reads* `messages.citations` back, and a stored
    #: row that predates a key must not cost the whole envelope its citations — which is what
    #: a required field would do, since a single invalid segment falls the list back to plain
    #: text. Found by `test_list_returns_the_transcript_in_the_fr_msg_06_shape`.
    page: str | None = None
    locator: CitationLocator | None = None
    quote: str
    chunkId: str  # noqa: N815 — FR-MSG-06 fixes the spelling
    score: float | None = Field(
        default=None,
        description=(
            "The FR-CIT-04 rerank score. **Absent** — not null — when the reranker failed open "
            "and published none (R-47(2)). Render the card with no number."
        ),
    )

    @model_serializer(mode="wrap")
    def _omit_absent_score(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Drop `score` entirely when there is none, which is point (3) of this module.

        Not `exclude_none`: `page` and `locator` are legitimately `null` and must keep their
        keys. A plain optional field would serialise `"score": null`, and "no score" and "a
        score of zero" are exactly what FR-CIT-04 needs a client to be able to tell apart.
        """
        data = handler(self)
        if data.get("score") is None:
            data.pop("score", None)
        return data


#: One run of the answer. **Not a tagged union**: `isCite` is *absent* on a text run and that is
#: the persisted JSONB shape, so giving :class:`TextSegment` an ``isCite: Literal[False]`` would
#: rewrite stored data to buy a discriminator it does not need. The two are unambiguous anyway —
#: each requires a field the other lacks. TypeScript narrows on ``'isCite' in seg``.
#:
#: The citation arm is first so Pydantic's smart union tries the more specific shape first.
type Segment = CitationSegment | TextSegment

#: Reused rather than rebuilt per call — a `TypeAdapter` compiles its validator on construction.
_SEGMENTS: TypeAdapter[list[Segment]] = TypeAdapter(list[Segment])


def scores_by_chunk_id(source_ids: Sequence[str], scores: Sequence[float]) -> dict[str, float]:
    """Map the position-aligned `rerank_scores` onto chunk ids.

    Empty unless the two sequences line up exactly — R-47(2) makes `rerank_scores` "empty or
    exactly as long as `reranked_chunk_ids`", so a mismatch is a state we do not understand,
    and inventing a partial mapping from it would publish some citations with a score and
    others without for no reason a user could see.
    """
    if not scores or len(scores) != len(source_ids):
        return {}
    return dict(zip(source_ids, scores, strict=True))


def build_citations(
    *,
    segments: Sequence[AnswerSegment],
    source_ids: Sequence[str],
    hits: Sequence[RetrievedChunk] = (),
    scores: Sequence[float] = (),
) -> tuple[CitationEnvelope, int]:
    """Resolve `segments` into the FR-MSG-06 envelope. Returns ``(envelope, dropped)``.

    ``dropped`` counts citations whose chunk was not in `hits` — telemetry, because a turn
    that loses citations between the gate and the persist step is a real event (a document
    deleted mid-answer) and would otherwise be invisible.

    Pure: no I/O, no clock. The caller does the reading, which is what lets `finalize` guard
    the database work and lets a test build an envelope from three literals.
    """
    by_id = {str(hit.chunk_id): hit for hit in hits}
    score_of = scores_by_chunk_id(source_ids, scores)

    out: list[dict[str, Any]] = []
    dropped = 0

    def add_text(text: str) -> None:
        """Append prose, merging into the previous run.

        Merging matters because a dropped citation leaves its neighbours adjacent: without
        this, `"the claim [S2] and"` would persist as two text segments the GUI renders with
        the chip's margins still between them. The doubled space a removal creates is
        collapsed at the seam — and only there, since the answer is what the user reads and
        FR-MSG-07 renders it as Markdown.
        """
        if not text:
            return
        if out and "text" in out[-1] and not out[-1].get("isCite"):
            previous = out[-1]["text"]
            if previous.endswith(" ") and text.startswith(" "):
                text = text[1:]
            out[-1]["text"] = previous + text
            return
        out.append({"text": text})

    for segment in segments:
        if segment.chunk_id is None:
            add_text(segment.text)
            continue
        hit = by_id.get(segment.chunk_id)
        if hit is None:
            # FR-CIT-06's "dropped before display", and dropped *silently* rather than left as
            # the literal `"[S2]"`: R-48(6) already established that a marker addressing
            # nothing is removed from the text, and a user cannot act on a bare marker any
            # more than on a dead chip.
            dropped += 1
            continue
        out.append(_citation(hit, score=score_of.get(segment.chunk_id)))

    if not out:
        # An answer that is entirely citations, all of which vanished. The text is still the
        # served answer, so the envelope keeps it rather than persisting an empty list the GUI
        # would render as a blank bubble.
        out.append({"text": "".join(s.text for s in segments if s.chunk_id is None)})

    envelope: CitationEnvelope = {
        SEGMENTS_KEY: out,
        SOURCE_IDS_KEY: [str(source_id) for source_id in source_ids],
    }
    return envelope, dropped


def _citation(hit: RetrievedChunk, *, score: float | None) -> dict[str, Any]:
    """One citation segment.

    `page` carries the R-34 rendered label ("p. 14", "§ Setup › Install", "rows 51–100") and
    `locator` the structured fields beside it. Both, because FR-CIT-04 is explicit that
    clients read the *fields* and never parse the label — and FR-MSG-06 names `page`, which
    only PDFs have. A DOCX citation therefore carries a section label under a key called
    `page`, which is the wire shape the spec fixed; the structured `locator` is what a client
    should branch on.

    `quote` is the chunk text verbatim — the denormalised copy R-36(6) sanctions, and the
    reason a replaced document's historical citation still renders after its chunk rows are
    gone.

    Built as a :class:`CitationSegment` and dumped, rather than assembled as a dict beside a
    model that describes it (T-405). A describing model drifts the first time someone edits the
    literal, and the drift is invisible because the schema still validates. The `score` key is
    still *absent* rather than null when the reranker published none — that rule now lives in
    the model's serializer, so it holds for every producer of this shape rather than for this
    function alone.
    """
    locator = hit.meta.get("locator")
    segment = CitationSegment(
        doc=hit.filename,
        page=hit.locator_label,
        locator=_locator(locator),
        quote=hit.chunk_text,
        chunkId=str(hit.chunk_id),
        score=score,
    )
    return segment.model_dump(mode="json")


def _locator(raw: object) -> CitationLocator | None:
    """Coerce the stored locator payload, degrading to `None` rather than failing.

    A locator this build cannot parse costs the citation its structured fields; refusing the
    whole citation would cost the user a chip over metadata FR-CIT-04 treats as an enrichment
    of the `page` label it sits beside.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        return CitationLocator.model_validate(dict(raw))
    except ValidationError:
        return None


def plain_segments(content: str) -> list[Segment]:
    """The envelope for an answer with no resolved citations.

    Used for abstentions, blocked turns and any pre-T-402 row: FR-MSG-06's `segs` is never
    empty for a message that has text, so the fallback is one text run rather than `[]`.
    """
    return [TextSegment(text=content)]


def envelope_segments(envelope: object, *, content: str) -> list[Segment]:
    """Read `segs` back out for the API DTO, tolerating anything that is not one.

    Defensive because this reads a JSONB column: a row written by an older build, or by a
    future one, must render as *the answer text* rather than as an error — the transcript is
    the user's own, and refusing to display it is the worst available outcome.

    **T-405 typed the return without narrowing that promise.** Validation moved *into* this
    function precisely so it could not move into the DTO, where a `ResponseValidationError`
    would turn a legacy row into a `500`. The tolerance is in fact wider than before: a Mapping
    that was not a valid segment previously sailed through as an untyped dict and broke the
    renderer at display time, and now falls back to the answer text here — which is what the
    paragraph above always promised.
    """
    if isinstance(envelope, Mapping):
        raw = envelope.get(SEGMENTS_KEY)
        if isinstance(raw, list) and raw:
            try:
                segments = _SEGMENTS.validate_python(raw)
            except ValidationError:
                return plain_segments(content)
            if segments:
                return segments
    return plain_segments(content)
