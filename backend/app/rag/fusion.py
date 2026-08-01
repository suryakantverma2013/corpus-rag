"""Rank fusion and overlap dedupe for hybrid retrieval (T-206, R-37, FR-RET-01).

Two pure functions, deliberately free of SQLAlchemy and of the session: the arms are
issued by `app.db.repositories.retrieval`, and everything that decides *ordering* lives
here so it can be unit-tested without Postgres.

**Why rank fusion and not a weighted score** (R-37(1)). The dense arm scores in cosine
similarity and the sparse arm in `ts_rank_cd`; the two are on unrelated scales, and
`ts_rank_cd`'s range additionally depends on the query and on which candidates happened to
match. Blending them needs a per-query min-max normalisation whose bounds move with the
result set, so the same chunk earns a different score for two near-identical queries and
the blend weight silently re-tunes itself as the corpus grows. Reciprocal Rank Fusion
throws the magnitudes away and keeps only the position, which is the part both arms agree
on the meaning of.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = ["FusedRank", "ProbeRank", "drop_overlapping_neighbours", "rrf_fuse", "rrf_merge"]


@dataclass(frozen=True, slots=True)
class FusedRank:
    """Where one chunk placed in each arm, and what that fused to.

    ``dense_rank`` / ``sparse_rank`` are 1-based, or ``None`` when the chunk did not appear
    in that arm at all. They are carried rather than discarded because "which arm found
    this" is the first question asked of any bad retrieval result, and because T-306 may
    want it as a rerank feature.
    """

    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None


def rrf_fuse(
    *,
    dense: Sequence[uuid.UUID],
    sparse: Sequence[uuid.UUID],
    k: int,
) -> dict[uuid.UUID, FusedRank]:
    """Reciprocal Rank Fusion over the two arms: ``Σ 1 / (k + rank)``.

    Each arm is an ordered sequence of chunk ids, best first. A chunk found by both arms
    accumulates both terms, which is the whole point — agreement between an embedding and a
    lexical match is the strongest signal either arm can give.

    ``k`` damps the head of the distribution: with ``k = 60`` the gap between ranks 1 and 2
    is small enough that one arm's top hit cannot by itself outrank a chunk both arms liked.
    A duplicate id within one arm is ignored after its first (best) occurrence — the arms
    are ``SELECT``s over a primary key so it cannot happen today, but silently
    double-counting would be an unpleasant way to find that out.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1")

    dense_ranks: dict[uuid.UUID, int] = {}
    for position, chunk_id in enumerate(dense, start=1):
        dense_ranks.setdefault(chunk_id, position)
    sparse_ranks: dict[uuid.UUID, int] = {}
    for position, chunk_id in enumerate(sparse, start=1):
        sparse_ranks.setdefault(chunk_id, position)

    fused: dict[uuid.UUID, FusedRank] = {}
    for chunk_id in list(dense_ranks) + [c for c in sparse_ranks if c not in dense_ranks]:
        dense_rank = dense_ranks.get(chunk_id)
        sparse_rank = sparse_ranks.get(chunk_id)
        score = 0.0
        if dense_rank is not None:
            score += 1.0 / (k + dense_rank)
        if sparse_rank is not None:
            score += 1.0 / (k + sparse_rank)
        fused[chunk_id] = FusedRank(score=score, dense_rank=dense_rank, sparse_rank=sparse_rank)
    return fused


@dataclass(frozen=True, slots=True)
class ProbeRank:
    """Where one chunk placed across the probes of a single turn (T-305, R-46(3)).

    ``best_rank`` and ``probe_count`` are carried for the reason `FusedRank` carries its
    per-arm ranks: "how many of the reformulations found this, and how highly" is the first
    question asked of a surprising merged ordering, and T-306 may want either as a feature.
    """

    score: float
    best_rank: int
    probe_count: int


def rrf_merge(
    lists: Sequence[Sequence[uuid.UUID]],
    *,
    k: int,
) -> dict[uuid.UUID, ProbeRank]:
    """Reciprocal Rank Fusion **across probes** — the same formula, one level up (R-46(3)).

    R-45(3) has retrieval search ``[query] + sub_queries``, so a turn produces up to four
    independently ranked lists where :func:`rrf_fuse` produces one. Merging them is a
    separate decision, and it is answered the same way for the same reason: the per-probe
    scores that come back are RRF sums, and adding *those* would reward whichever probe
    happened to match a lot of chunks. Positions are the only thing the lists agree on the
    meaning of, so positions are what is fused. Agreement across reformulations then earns
    exactly what agreement between the dense and sparse arms earns — a chunk three probes
    place near the top outranks a chunk one probe placed first.

    **Every probe carries the same weight, including the original query.** Weighting the
    user's own words above the router's rewrites is defensible and is deliberately not done
    here: the weight would be a §8.4 knob nobody can settle without a retrieval-quality
    corpus, and R-45(3) already guarantees the original query is *always* one of the lists,
    which is the protection a weight would be buying. Revisit with that corpus, not before.

    Ordering is left to the caller — the returned mapping is keyed by chunk id because a
    representative object per id is the caller's to pick (`rrf_fuse` returns the same shape
    for the same reason). A duplicate id within one probe's list counts once, at its best
    position.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1")

    merged: dict[uuid.UUID, ProbeRank] = {}
    for probe in lists:
        seen: set[uuid.UUID] = set()
        for position, chunk_id in enumerate(probe, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            previous = merged.get(chunk_id)
            contribution = 1.0 / (k + position)
            if previous is None:
                merged[chunk_id] = ProbeRank(score=contribution, best_rank=position, probe_count=1)
            else:
                merged[chunk_id] = ProbeRank(
                    score=previous.score + contribution,
                    best_rank=min(previous.best_rank, position),
                    probe_count=previous.probe_count + 1,
                )
    return merged


class _Neighbourly(Protocol):
    """What :func:`drop_overlapping_neighbours` needs from a candidate.

    Structural rather than nominal so the function works on `RetrievedChunk` without this
    module importing it — fusion must not depend on the retrieval contract, only the
    other way round.
    """

    @property
    def document_id(self) -> uuid.UUID: ...
    @property
    def block_order(self) -> int | None: ...
    @property
    def block_chunk_index(self) -> int | None: ...


def drop_overlapping_neighbours[T: _Neighbourly](candidates: Iterable[T]) -> list[T]:
    """Discard the weaker half of every overlapping adjacent pair (R-35(5), R-37(6)).

    ``candidates`` must already be ordered best-first; the survivor of a pair is whichever
    came first, so this must run *after* fusion, not before either arm.

    R-35(5) accepted that a chunk shares up to `CHUNKER_OVERLAP_CHARS` with the next chunk
    **of the same block**, and flagged the consequence for this task: FTS indexes that
    shared text twice, so a query matching only the overlapping region ranks both chunks
    highly and burns two of the reranker's slots — and later two FR-CIT-03 citations — on
    one passage.

    Adjacency is keyed on ``(document_id, block_order, block_chunk_index)`` from the
    R-35(12) metadata contract, **never** on ``chunk_index``. Overlap never crosses a block
    boundary (R-35(5)), so two chunks with consecutive `chunk_index` either side of one
    share nothing, and dropping either would lose real content. Candidates missing the
    metadata (defensively: a chunk written before the contract existed) are always kept —
    an unknown neighbour relationship is not a reason to delete a search result.
    """
    kept: list[T] = []
    seen: set[tuple[uuid.UUID, int, int]] = set()
    for candidate in candidates:
        block_order = candidate.block_order
        block_chunk_index = candidate.block_chunk_index
        if block_order is None or block_chunk_index is None:
            kept.append(candidate)
            continue
        key = (candidate.document_id, block_order, block_chunk_index)
        neighbours = (
            (candidate.document_id, block_order, block_chunk_index - 1),
            (candidate.document_id, block_order, block_chunk_index + 1),
        )
        if any(neighbour in seen for neighbour in neighbours):
            continue
        seen.add(key)
        kept.append(candidate)
    return kept
