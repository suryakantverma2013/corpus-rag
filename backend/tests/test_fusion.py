"""Rank fusion + overlap dedupe (T-206, R-37). Pure — no database.

Everything that decides the *order* of hybrid results is tested here so a ranking
regression is caught by a millisecond of arithmetic rather than by a DB-backed test that
skips when Postgres is down. The wiring itself lives in `tests/test_retrieval.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.rag.fusion import drop_overlapping_neighbours, rrf_fuse

K = 60


def _ids(count: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=index + 1) for index in range(count)]


# --- RRF ----------------------------------------------------------------------


def test_rrf_scores_are_the_sum_of_reciprocal_ranks() -> None:
    a, b = _ids(2)
    fused = rrf_fuse(dense=[a, b], sparse=[b, a], k=K)

    assert fused[a].score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[a].dense_rank == 1
    assert fused[a].sparse_rank == 2
    assert fused[b].dense_rank == 2
    assert fused[b].sparse_rank == 1


def test_agreement_between_arms_beats_a_single_arms_top_hit() -> None:
    """The property the whole design rests on: two weak agreeing signals beat one strong one."""
    both, dense_only = _ids(2)
    # `both` places 3rd and 3rd; `dense_only` is the dense arm's outright winner.
    fused = rrf_fuse(
        dense=[dense_only, uuid.uuid4(), both],
        sparse=[uuid.uuid4(), uuid.uuid4(), both],
        k=K,
    )
    assert fused[both].score > fused[dense_only].score


def test_a_chunk_from_one_arm_only_keeps_the_other_rank_none() -> None:
    a, b = _ids(2)
    fused = rrf_fuse(dense=[a], sparse=[b], k=K)

    assert fused[a].sparse_rank is None
    assert fused[b].dense_rank is None
    assert fused[a].score == pytest.approx(fused[b].score)


def test_empty_arms_fuse_to_nothing() -> None:
    assert rrf_fuse(dense=[], sparse=[], k=K) == {}


def test_single_arm_preserves_that_arms_order() -> None:
    a, b, c = _ids(3)
    fused = rrf_fuse(dense=[a, b, c], sparse=[], k=K)
    ranked = sorted(fused, key=lambda chunk_id: -fused[chunk_id].score)
    assert ranked == [a, b, c]


def test_a_repeated_id_within_one_arm_is_counted_once_at_its_best_rank() -> None:
    a, b = _ids(2)
    fused = rrf_fuse(dense=[a, b, a], sparse=[], k=K)
    assert fused[a].dense_rank == 1
    assert fused[a].score == pytest.approx(1 / 61)


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="RRF k"):
        rrf_fuse(dense=[], sparse=[], k=0)


def test_larger_k_flattens_the_gap_between_adjacent_ranks() -> None:
    a, b = _ids(2)
    tight = rrf_fuse(dense=[a, b], sparse=[], k=1)
    loose = rrf_fuse(dense=[a, b], sparse=[], k=600)
    assert tight[a].score - tight[b].score > loose[a].score - loose[b].score


# --- adjacent-overlap dedupe (R-35(5) / R-37(6)) ------------------------------


@dataclass(frozen=True, slots=True)
class _Hit:
    """Minimal stand-in for `RetrievedChunk`'s dedupe-relevant surface."""

    document_id: uuid.UUID
    block_order: int | None
    block_chunk_index: int | None
    label: str = ""


DOC = uuid.UUID(int=100)
OTHER_DOC = uuid.UUID(int=200)


def test_adjacent_chunks_of_one_block_lose_the_lower_ranked_one() -> None:
    hits = [_Hit(DOC, 0, 3, "kept"), _Hit(DOC, 0, 4, "overlaps")]
    assert [hit.label for hit in drop_overlapping_neighbours(hits)] == ["kept"]


def test_the_survivor_is_whichever_ranked_higher_not_whichever_came_first_in_the_document() -> None:
    """Input order is fusion order, so the later chunk wins when it ranked better."""
    hits = [_Hit(DOC, 0, 4, "kept"), _Hit(DOC, 0, 3, "overlaps")]
    assert [hit.label for hit in drop_overlapping_neighbours(hits)] == ["kept"]


def test_non_adjacent_chunks_of_the_same_block_both_survive() -> None:
    hits = [_Hit(DOC, 0, 1, "a"), _Hit(DOC, 0, 3, "b")]
    assert [hit.label for hit in drop_overlapping_neighbours(hits)] == ["a", "b"]


def test_consecutive_indexes_in_different_blocks_both_survive() -> None:
    """Overlap never crosses a block boundary (R-35(5)), so this pair shares no text."""
    hits = [_Hit(DOC, 0, 5, "end of block 0"), _Hit(DOC, 1, 6, "start of block 1")]
    assert len(drop_overlapping_neighbours(hits)) == 2


def test_same_block_and_index_in_different_documents_both_survive() -> None:
    hits = [_Hit(DOC, 0, 1, "a"), _Hit(OTHER_DOC, 0, 2, "b")]
    assert len(drop_overlapping_neighbours(hits)) == 2


def test_a_chunk_without_block_metadata_is_kept() -> None:
    """An unknown neighbour relationship is not a reason to delete a search result."""
    hits = [_Hit(DOC, None, None, "a"), _Hit(DOC, None, None, "b")]
    assert len(drop_overlapping_neighbours(hits)) == 2


def test_a_run_of_three_adjacent_chunks_keeps_the_first_and_the_last() -> None:
    """Dropping 4 unblocks 5: it neighbours only the chunk that was already discarded."""
    hits = [_Hit(DOC, 0, 3, "a"), _Hit(DOC, 0, 4, "b"), _Hit(DOC, 0, 5, "c")]
    assert [hit.label for hit in drop_overlapping_neighbours(hits)] == ["a", "c"]
