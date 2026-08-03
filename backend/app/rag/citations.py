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
from typing import Any

from app.rag.generation import AnswerSegment
from app.rag.retrieval import RetrievedChunk

__all__ = [
    "SEGMENTS_KEY",
    "SOURCE_IDS_KEY",
    "CitationEnvelope",
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
    """
    locator = hit.meta.get("locator")
    segment: dict[str, Any] = {
        "isCite": True,
        "doc": hit.filename,
        "page": hit.locator_label,
        "locator": dict(locator) if isinstance(locator, Mapping) else None,
        "quote": hit.chunk_text,
        "chunkId": str(hit.chunk_id),
    }
    if score is not None:
        # Present only when the reranker published one (R-47(2)). The key is *absent*, not
        # null, so "no score" and "a score of zero" cannot be confused by a client reading it.
        segment["score"] = score
    return segment


def plain_segments(content: str) -> list[dict[str, Any]]:
    """The envelope for an answer with no resolved citations.

    Used for abstentions, blocked turns and any pre-T-402 row: FR-MSG-06's `segs` is never
    empty for a message that has text, so the fallback is one text run rather than `[]`.
    """
    return [{"text": content}]


def envelope_segments(envelope: object, *, content: str) -> list[dict[str, Any]]:
    """Read `segs` back out for the API DTO, tolerating anything that is not one.

    Defensive because this reads a JSONB column: a row written by an older build, or by a
    future one, must render as *the answer text* rather than as an error — the transcript is
    the user's own, and refusing to display it is the worst available outcome.
    """
    if isinstance(envelope, Mapping):
        segments = envelope.get(SEGMENTS_KEY)
        if isinstance(segments, list) and segments:
            return [dict(s) for s in segments if isinstance(s, Mapping)] or plain_segments(content)
    return plain_segments(content)
