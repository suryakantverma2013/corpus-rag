"""Hybrid retriever — pgvector dense + Postgres FTS sparse, RRF-fused (T-206, R-37).

Implements FR-RET-01's committed default. `HybridRetriever` runs two arms over the same
access-filtered row set and fuses them by rank; `PgVectorRetriever` keeps the dense-only
form FR-RET-01 sanctions as a fallback. Both are `app.rag.retrieval.Retriever` (OI-18).

**Every access filter is in the query** (FR-RET-04 / NFR-SEC-06) and both arms build theirs
from the single :func:`_access_predicates` helper, so the two cannot drift apart — a
predicate added to one arm only would be a hole open exactly half the time.

Two things in here look like style choices and are not:

* The dense arm orders on ``embedding::halfvec(3072) <=> …``, not on the bare ``vector``
  operator. `ix_document_chunks_embedding` is built on the **cast** expression (3072 dims
  exceed pgvector's 2000-dim ceiling for indexing `vector` directly), and Postgres serves a
  functional index only for a query whose expression matches it. The uncast form is
  correct, returns identical rows, and sequentially scans every chunk in the tenant.
* The FTS configuration is rendered **inline** as `'english'` rather than bound as a
  parameter, for the same reason: `ix_document_chunks_chunk_text_fts` indexes
  ``to_tsvector('english', chunk_text)``, and a bind parameter is not a constant at plan
  time, so the parameterised form never matches the index either.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    cast,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy import func as sql_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RetrievalSettings, get_settings
from app.db.base import EMBEDDING_DIM
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.rag.fusion import drop_overlapping_neighbours, rrf_fuse
from app.rag.retrieval import RetrievalFilter, RetrievedChunk

log = structlog.get_logger(__name__)

__all__ = [
    "HybridRetriever",
    "PgVectorRetriever",
    "dense_distance",
    "fts_document",
    "fts_query",
    "websearch_input",
]

#: pgvector's default `hnsw.ef_search`. An HNSW scan cannot return more neighbours than its
#: search list holds, so fetching more candidates than this without raising it silently
#: truncates the dense arm.
_PGVECTOR_DEFAULT_EF_SEARCH = 40

#: Text-search configurations this module will inline into SQL. `RetrievalSettings` already
#: pins the value; this is the second lock, because the string is concatenated into a
#: statement rather than bound.
_ALLOWED_FTS_CONFIGS = frozenset({"english"})

#: What every arm selects. Ordered to match `_row_to_candidate`.
_PROJECTION = (
    DocumentChunk.id,
    DocumentChunk.document_id,
    DocumentChunk.knowledge_base_id,
    DocumentChunk.chunk_index,
    DocumentChunk.chunk_text,
    DocumentChunk.meta,
    Document.filename,
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One arm's hit, before fusion."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    chunk_index: int
    chunk_text: str
    meta: Mapping[str, Any]
    filename: str
    score: float


def _row_to_candidate(row: Row[Any], score: float) -> _Candidate:
    return _Candidate(
        chunk_id=row[0],
        document_id=row[1],
        knowledge_base_id=row[2],
        chunk_index=row[3],
        chunk_text=row[4],
        meta=row[5] or {},
        filename=row[6],
        score=score,
    )


# --- access control -----------------------------------------------------------


def _access_predicates(filters: RetrievalFilter) -> list[ColumnElement[bool]]:
    """The FR-RET-04 / NFR-SEC-06 filter set, shared by every arm of every retriever.

    `document_chunks` alone cannot express this. `searchable` and `deleted_at` live on
    `documents`, and they are the two that make FR-ING-05 deletion take effect on the very
    next query — without the join a soft-deleted document keeps answering.

    The `document_version = current_version` predicate is defence in depth. R-36(4) as
    amended deletes the superseded version inside the swap transaction, so in the normal
    path no stale row exists to exclude; this keeps the invariant if a restored backup or
    some future write path ever leaves one behind, and it costs nothing — the join is
    already required.

    **It does bind T-207** (R-37(9)): `documents.current_version` must be advanced in the
    same transaction as the chunk swap. `persist_chunk_set` deliberately does not touch it,
    so a T-207 that writes the v2 rows and forgets the pointer leaves a document that is
    `ACTIVE`, listed in the KB, and retrieves nothing at all.
    """
    predicates: list[ColumnElement[bool]] = [
        DocumentChunk.tenant_id == filters.tenant_id,
        Document.tenant_id == filters.tenant_id,
        Document.owner_id == filters.owner_id,
        Document.searchable.is_(True),
        Document.deleted_at.is_(None),
        DocumentChunk.document_version == Document.current_version,
    ]
    if filters.active_only:
        predicates.append(DocumentChunk.is_active.is_(True))
    if filters.knowledge_base_ids:
        predicates.append(DocumentChunk.knowledge_base_id.in_(list(filters.knowledge_base_ids)))
    if filters.document_ids:
        predicates.append(DocumentChunk.document_id.in_(list(filters.document_ids)))
    return predicates


def _access_filtered_select(
    filters: RetrievalFilter, score: ColumnElement[Any]
) -> Select[tuple[Any, ...]]:
    """`SELECT <projection>, <score> FROM document_chunks JOIN documents WHERE <access>`."""
    return (
        select(*_PROJECTION, score)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*_access_predicates(filters))
    )


# --- sparse-arm query text ----------------------------------------------------


def websearch_input(query_text: str) -> str:
    """Turn a user question into a `websearch_to_tsquery` string that **ORs** its terms.

    The default text-search parsers all AND: `plainto_tsquery`, and `websearch_to_tsquery`
    on bare input, both require *every* lexeme to be present. A fifteen-word natural
    question therefore matches nothing, and hybrid retrieval quietly degenerates into
    dense-only while looking perfectly wired up. Joining the terms with ``OR`` restores
    recall and lets `ts_rank_cd` do the discriminating — it already rewards chunks that
    match more of the terms, closer together, which is the job a hard AND was doing badly.

    Two `websearch_to_tsquery` operators change meaning rather than narrow it and are
    stripped per token: a leading ``-`` becomes negation (``!foo`` OR-ed with the rest
    matches nearly the whole corpus), and ``"`` opens a phrase that would swallow the
    following terms. Everything else is left alone — `websearch_to_tsquery` is documented
    never to raise on malformed input, and it drops stop-words itself, so an all-stop-word
    question yields the empty tsquery, which matches nothing and leaves dense-only results
    standing (the FR-RET-01 fallback, reached by construction rather than by a branch).
    """
    terms = [token.lstrip("-").replace('"', "") for token in query_text.split()]
    return " OR ".join(term for term in terms if term)


def _fts_config(settings: RetrievalSettings) -> ColumnElement[Any]:
    """The text-search config, inlined so the functional index can match — see module doc."""
    if settings.fts_language not in _ALLOWED_FTS_CONFIGS:  # pragma: no cover — validator first
        raise ValueError(f"unsupported RETRIEVAL_FTS_LANGUAGE {settings.fts_language!r}")
    return literal_column(f"'{settings.fts_language}'")


# --- indexed expressions ------------------------------------------------------
#
# These three are the expressions the T-101 indexes are built on. They are named,
# module-level and shared by the arms and by the tests that `EXPLAIN` them, because both
# indexes are *functional*: they serve a query only when its expression matches theirs
# character for character after parsing. Inlining either of these at a call site is how the
# index silently stops being used, which costs correct answers nothing and latency
# everything — the failure this indirection exists to make loud.


def dense_distance(query_embedding: Sequence[float]) -> ColumnElement[Any]:
    """Cosine distance over the `halfvec` cast — matches `ix_document_chunks_embedding`."""
    return cast(DocumentChunk.embedding, HALFVEC(EMBEDDING_DIM)).cosine_distance(
        literal(list(query_embedding), HALFVEC(EMBEDDING_DIM))
    )


def fts_document(settings: RetrievalSettings) -> ColumnElement[Any]:
    """`to_tsvector('english', chunk_text)` — matches `ix_document_chunks_chunk_text_fts`."""
    return sql_func.to_tsvector(_fts_config(settings), DocumentChunk.chunk_text)


def fts_query(websearch: str, settings: RetrievalSettings) -> ColumnElement[Any]:
    """The OR-ed tsquery. The user's text is **bound**, only the config is inlined."""
    return sql_func.websearch_to_tsquery(_fts_config(settings), literal(websearch))


# --- arms ---------------------------------------------------------------------


async def _dense_arm(
    session: AsyncSession,
    query_embedding: Sequence[float],
    *,
    filters: RetrievalFilter,
    limit: int,
    ef_search: int,
) -> list[_Candidate]:
    """Cosine nearest neighbours over `document_chunks.embedding` (FR-RET-01 dense half)."""
    if ef_search > _PGVECTOR_DEFAULT_EF_SEARCH:
        # SET LOCAL, so it reverts with the caller's transaction rather than leaking into
        # whatever else reuses this session.
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))

    distance = dense_distance(query_embedding).label("distance")
    stmt = (
        _access_filtered_select(filters, distance)
        .where(DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [_row_to_candidate(row, 1.0 - float(row[-1])) for row in rows]


async def _sparse_arm(
    session: AsyncSession,
    query_text: str,
    *,
    filters: RetrievalFilter,
    limit: int,
    settings: RetrievalSettings,
) -> list[_Candidate]:
    """Lexical matches ranked by `ts_rank_cd` (FR-RET-01 sparse half, R-37(2)).

    Cover density rather than plain `ts_rank`: it accounts for how close the matched
    lexemes sit to each other, which is the closest thing core Postgres offers to the
    phrase sensitivity a BM25 implementation would bring. This is **not** BM25 — no IDF
    term weighting, no document-length saturation — and R-37(2) records that deviation from
    FR-RET-01's wording against the cost of an extra database extension.
    """
    websearch = websearch_input(query_text)
    if not websearch:
        return []

    document = fts_document(settings)
    tsquery = fts_query(websearch, settings)
    rank = sql_func.ts_rank_cd(document, tsquery).label("rank")

    stmt = (
        _access_filtered_select(filters, rank)
        .where(document.bool_op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [_row_to_candidate(row, float(row[-1])) for row in rows]


# --- retrievers ---------------------------------------------------------------


class PgVectorRetriever:
    """Dense-only `Retriever` — FR-RET-01's sanctioned fallback, and the T-306 test seam."""

    def __init__(self, session: AsyncSession, settings: RetrievalSettings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings().retrieval

    async def search(
        self,
        query_text: str,
        query_embedding: Sequence[float],
        *,
        filters: RetrievalFilter,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        limit = top_k or self.settings.fusion_top_k
        candidates = await _dense_arm(
            self.session,
            query_embedding,
            filters=filters,
            limit=limit,
            ef_search=self.settings.hnsw_ef_search,
        )
        return [
            RetrievedChunk(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                knowledge_base_id=candidate.knowledge_base_id,
                filename=candidate.filename,
                chunk_index=candidate.chunk_index,
                chunk_text=candidate.chunk_text,
                score=candidate.score,
                meta=candidate.meta,
                dense_rank=rank,
                dense_score=candidate.score,
            )
            for rank, candidate in enumerate(candidates, start=1)
        ]


class HybridRetriever:
    """Dense + sparse, fused by RRF (FR-RET-01 committed default, R-37).

    Returns `RETRIEVAL_FUSION_TOP_K` candidates — the **reranker's** input, not the
    FR-RET-02 top-K, which counts reranked passages and belongs to T-306.
    """

    def __init__(self, session: AsyncSession, settings: RetrievalSettings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings().retrieval

    async def search(
        self,
        query_text: str,
        query_embedding: Sequence[float],
        *,
        filters: RetrievalFilter,
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        settings = self.settings
        limit = top_k or settings.fusion_top_k

        # Sequential, not concurrent: both arms share one AsyncSession, and a session is
        # not safe for concurrent statements.
        dense = await _dense_arm(
            self.session,
            query_embedding,
            filters=filters,
            limit=settings.dense_candidates,
            ef_search=settings.hnsw_ef_search,
        )
        sparse = await _sparse_arm(
            self.session,
            query_text,
            filters=filters,
            limit=settings.sparse_candidates,
            settings=settings,
        )

        fused = rrf_fuse(
            dense=[candidate.chunk_id for candidate in dense],
            sparse=[candidate.chunk_id for candidate in sparse],
            k=settings.rrf_k,
        )
        by_id = {candidate.chunk_id: candidate for candidate in dense}
        by_id.update({c.chunk_id: c for c in sparse if c.chunk_id not in by_id})
        dense_scores = {candidate.chunk_id: candidate.score for candidate in dense}
        sparse_scores = {candidate.chunk_id: candidate.score for candidate in sparse}

        hits = [
            RetrievedChunk(
                chunk_id=chunk_id,
                document_id=by_id[chunk_id].document_id,
                knowledge_base_id=by_id[chunk_id].knowledge_base_id,
                filename=by_id[chunk_id].filename,
                chunk_index=by_id[chunk_id].chunk_index,
                chunk_text=by_id[chunk_id].chunk_text,
                score=rank.score,
                meta=by_id[chunk_id].meta,
                dense_rank=rank.dense_rank,
                sparse_rank=rank.sparse_rank,
                dense_score=dense_scores.get(chunk_id),
                sparse_score=sparse_scores.get(chunk_id),
            )
            for chunk_id, rank in fused.items()
        ]
        # Ties broken by chunk id so an identical query returns an identical order —
        # a reranker fed a non-deterministic candidate order is untestable.
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id.bytes))

        if settings.dedupe_adjacent:
            deduped = drop_overlapping_neighbours(hits)
        else:
            deduped = hits

        log.debug(
            "retrieval.hybrid",
            dense=len(dense),
            sparse=len(sparse),
            fused=len(hits),
            dropped_overlap=len(hits) - len(deduped),
            returned=min(len(deduped), limit),
        )
        return deduped[:limit]
