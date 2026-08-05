"""The FR-MSG-06 citation envelope (T-402, R-47(2), R-48(4)/(6), R-49(3), R-50(5)).

Pure — no database, no network. `build_citations` takes segments the caller already split and
chunk rows the caller already read, which is what makes the two failure modes that matter
testable from literals: a citation whose chunk has vanished, and a grounding set the reranker
published no score for.

The round-trip against `workers/evaluate.py` is the load-bearing test here. The envelope is a
**contract** between two tasks that never call each other, and the failure mode if it drifts is
silent: the worker reads no citations, logs a skip, and every message keeps `evaluation` NULL
for ever — which looks exactly like the judge being unreachable, an outcome R-50(3) makes
legitimate.
"""

from __future__ import annotations

import uuid

from app.rag.citations import (
    SEGMENTS_KEY,
    SOURCE_IDS_KEY,
    TextSegment,
    build_citations,
    envelope_segments,
    scores_by_chunk_id,
)
from app.rag.generation import split_answer_segments
from app.rag.retrieval import RetrievedChunk
from workers.evaluate import _cited_ids


def _chunk(chunk_id: uuid.UUID, *, text: str = "Refunds within 30 days.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="policy.pdf",
        chunk_index=0,
        chunk_text=text,
        score=1.0,
        meta={"locator": {"kind": "page", "page": 4, "label": "p. 4"}},
    )


def test_a_citation_carries_the_hover_card_s_fields() -> None:
    """FR-CIT-03's card is rendered from this payload, so every field it names is here.

    `page` holds the R-34 *label* and `locator` the structured fields beside it: FR-CIT-04 is
    explicit that clients read the fields and never parse the label, and only PDFs have pages
    at all.
    """
    chunk_id = uuid.uuid4()
    segments, dropped = split_answer_segments("The window is 30 days [S1].", [str(chunk_id)])
    assert dropped == 0

    envelope, lost = build_citations(
        segments=segments,
        source_ids=[str(chunk_id)],
        hits=[_chunk(chunk_id)],
        scores=[0.87],
    )

    assert lost == 0
    citation = envelope[SEGMENTS_KEY][1]
    assert citation["isCite"] is True
    assert citation["doc"] == "policy.pdf"
    assert citation["page"] == "p. 4"
    assert citation["locator"]["kind"] == "page"
    assert citation["quote"] == "Refunds within 30 days."
    assert citation["chunkId"] == str(chunk_id)
    assert citation["score"] == 0.87
    assert envelope[SOURCE_IDS_KEY] == [str(chunk_id)]


def test_a_vanished_chunk_loses_its_chip_and_the_prose_closes_over_it() -> None:
    """FR-CIT-06 (1)/(5) — "dropped before display", discharged by this read (R-49(3)).

    The gate does no database work at all on the argument that the persist-time read happens a
    superstep later and catches exactly this: a document deleted between the answer being
    generated and the row being written. Rendering the chip would address a passage that no
    longer exists; keeping the literal `"[S1]"` would show a user a marker they cannot act on
    (R-48(6) settled the same question for an out-of-range marker).
    """
    chunk_id = uuid.uuid4()
    segments, _ = split_answer_segments(
        "The window is 30 days [S1] and applies to all.", [str(chunk_id)]
    )

    envelope, lost = build_citations(segments=segments, source_ids=[str(chunk_id)], hits=[])

    assert lost == 1
    assert envelope[SEGMENTS_KEY] == [{"text": "The window is 30 days and applies to all."}], (
        "the neighbouring prose is joined, and the doubled space collapsed"
    )
    # `source_ids` still records the grounding set — the worker needs it to replay the split
    # (R-50(5)), and it is not recoverable from the segments.
    assert envelope[SOURCE_IDS_KEY] == [str(chunk_id)]


def test_no_rerank_score_means_no_score_key_at_all() -> None:
    """R-47(2) — the FR-CIT-04 score may be **absent**, and absent is not zero.

    The reranker fails open and publishes nothing; the R-46(3) cross-probe RRF score must not
    be substituted, because it is comparable within a turn only. A `0.0` reads to a user as
    "this passage is irrelevant", which is a claim we have no evidence for.
    """
    chunk_id = uuid.uuid4()
    segments, _ = split_answer_segments("Yes [S1].", [str(chunk_id)])

    envelope, _ = build_citations(
        segments=segments, source_ids=[str(chunk_id)], hits=[_chunk(chunk_id)], scores=[]
    )

    assert "score" not in envelope[SEGMENTS_KEY][1]


def test_a_score_list_that_does_not_line_up_publishes_no_scores() -> None:
    """R-47(2) makes `rerank_scores` "empty or exactly as long as `reranked_chunk_ids`".

    Anything else is a state we do not understand, and guessing a partial mapping from it would
    show a score on some citations and not others for no reason a user could see.
    """
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    assert scores_by_chunk_id(ids, [0.9]) == {}
    assert scores_by_chunk_id(ids, [0.9, 0.4]) == {ids[0]: 0.9, ids[1]: 0.4}


def test_the_envelope_round_trips_through_the_evaluation_worker() -> None:
    """The T-309 contract, asserted from both ends (R-50(5)).

    `workers/evaluate.py` reads `segments[].chunkId` to decide which passages to score and
    skips a message that cites nothing. Nothing else joins these two tasks, and a rename here
    would surface as "no message is ever evaluated" — indistinguishable from the judge being
    unreachable, which R-50(3) makes a legitimate end state.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    source_ids = [str(first), str(second)]
    segments, _ = split_answer_segments("A [S1] and B [S2], plus A again [S1].", source_ids)

    envelope, _ = build_citations(
        segments=segments,
        source_ids=source_ids,
        hits=[_chunk(first), _chunk(second)],
    )

    assert _cited_ids(envelope) == (first, second), "distinct, in first-citation order"


def test_an_uncited_answer_produces_no_citations_and_is_skipped_by_the_worker() -> None:
    """An abstention or a decline cites nothing, which is what excludes it from evaluation.

    Faithfulness against an empty context is not a low score, it is a meaningless one — so the
    worker's `no_citations` skip is the honest boundary, and it depends on this envelope
    genuinely being empty of citations rather than carrying placeholders.
    """
    segments, _ = split_answer_segments("I can't ground an answer to that.", [])

    envelope, lost = build_citations(segments=segments, source_ids=[])

    assert lost == 0
    assert envelope[SEGMENTS_KEY] == [{"text": "I can't ground an answer to that."}]
    assert envelope[SOURCE_IDS_KEY] == []
    assert _cited_ids(envelope) == ()


def test_an_unreadable_envelope_still_renders_the_answer_text() -> None:
    """A row written by an older build must display, not error.

    The transcript is the user's own; refusing to render it is the worst available outcome,
    and `messages.citations` is a JSONB column that predates this shape.

    T-405 typed the return without narrowing that promise: validation moved *into*
    `envelope_segments` precisely so a strict DTO could not turn a legacy row into a `500`.
    """
    fallback = [TextSegment(text="hello")]
    assert envelope_segments(None, content="hello") == fallback
    assert envelope_segments({"segments": []}, content="hello") == fallback
    assert envelope_segments({"nope": 1}, content="hello") == fallback


def test_a_segment_that_is_neither_shape_falls_back_to_the_answer_text() -> None:
    """The tolerance is now *wider* than it was, which is worth pinning (T-405).

    A Mapping that is not a valid segment previously sailed through as an untyped dict and
    broke the renderer at display time. It now falls back here — which is what the docstring
    above always promised.
    """
    assert envelope_segments({"segments": [{"weird": 1}]}, content="hello") == [
        TextSegment(text="hello")
    ]
    # A citation missing half its fields is not half-rendered either.
    assert envelope_segments({"segments": [{"isCite": True, "doc": "d.pdf"}]}, content="x") == [
        TextSegment(text="x")
    ]
