"""Hybrid retrieval against the real database (T-206, FR-RET-01/04, NFR-SEC-06).

Runs on the transactional-rollback `session` fixture, so it skips when Postgres is
unreachable. Ranking arithmetic is tested without a DB in `tests/test_fusion.py`; what is
tested here is the part only Postgres can answer — that the two arms return what they are
supposed to, that the access filters are actually in the query, and that both functional
indexes are still matched by the expressions the code emits.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RetrievalSettings
from app.db.base import DEFAULT_TENANT_ID, EMBEDDING_DIM
from app.db.enums import DocumentStatus
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import (
    HybridRetriever,
    PgVectorRetriever,
    dense_distance,
    fetch_chunks,
    fts_document,
    fts_query,
    websearch_input,
)
from app.db.repositories.users import UserRepository
from app.rag.retrieval import RetrievalFilter, Retriever

#: A token no English stemmer will relate to anything else, and that a random embedding
#: cannot possibly place near the query — the sparse arm is the only way to reach it.
RARE_TOKEN = "ERR-4021"


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _onehot(index: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[index] = 1.0
    return vec


def _settings(**overrides: object) -> RetrievalSettings:
    return RetrievalSettings(**overrides)  # type: ignore[arg-type]


async def _make_user(session: AsyncSession):  # noqa: ANN202
    return await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )


async def _make_kb(session: AsyncSession, owner):  # noqa: ANN001, ANN202
    return await KnowledgeBaseRepository(session).get_or_create_default(owner.id)


async def _make_document(
    session: AsyncSession,
    owner,  # noqa: ANN001
    kb,  # noqa: ANN001
    *,
    searchable: bool = True,
    deleted_at: datetime | None = None,
    current_version: int = 1,
) -> Document:
    doc = Document(
        owner_id=owner.id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        storage_uri="s3://corpus/handbook.pdf",
        checksum_sha256=_sha(uuid.uuid4().hex),
        status=DocumentStatus.ACTIVE,
        searchable=searchable,
        current_version=current_version,
    )
    doc.deleted_at = deleted_at
    return await DocumentRepository(session).add(doc)


async def _make_chunk(
    session: AsyncSession,
    doc,  # noqa: ANN001
    kb,  # noqa: ANN001
    *,
    index: int,
    text_: str,
    embedding: list[float] | None = None,
    document_version: int = 1,
    block_order: int | None = None,
    block_chunk_index: int | None = None,
) -> DocumentChunk:
    meta: dict[str, object] = {}
    if block_order is not None:
        meta = {"block_order": block_order, "block_chunk_index": block_chunk_index}
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version=document_version,
        chunk_index=index,
        chunk_hash=_sha(text_),
        embedding_fingerprint=_sha(f"fp:{doc.id}:{index}"),
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        chunk_text=text_,
        embedding=embedding,
        meta=meta,
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def _corpus(session: AsyncSession):  # noqa: ANN202
    """Three chunks: one the dense arm loves, one only the sparse arm can find, one neither."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    await _make_chunk(
        session, doc, kb, index=0, text_="general onboarding guidance", embedding=_onehot(0)
    )
    await _make_chunk(
        session,
        doc,
        kb,
        index=1,
        text_=f"the {RARE_TOKEN} fault clears after a controller reset",
        embedding=_onehot(1),
    )
    await _make_chunk(session, doc, kb, index=2, text_="unrelated filler", embedding=_onehot(2))
    return user, kb, doc


# --- protocol -----------------------------------------------------------------


async def test_hybrid_retriever_conforms_to_protocol(session: AsyncSession) -> None:
    assert isinstance(HybridRetriever(session), Retriever)


# --- the two arms -------------------------------------------------------------


async def test_sparse_arm_reaches_a_chunk_the_dense_arm_never_returns(
    session: AsyncSession,
) -> None:
    """The test hybrid retrieval exists to pass, and dense-only cannot.

    The dense arm is capped at one candidate and the query vector matches chunk 0, so the
    `ERR-4021` chunk is outside the dense arm's reach entirely — the only path to it is the
    lexical one.
    """
    user, kb, _ = await _corpus(session)
    filters = RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id])
    settings = _settings(dense_candidates=1)

    hits = await HybridRetriever(session, settings).search(RARE_TOKEN, _onehot(0), filters=filters)
    assert {hit.chunk_index for hit in hits} == {0, 1}

    dense_only = await PgVectorRetriever(session, settings).search(
        RARE_TOKEN, _onehot(0), filters=filters, top_k=1
    )
    assert [hit.chunk_index for hit in dense_only] == [0]


async def test_dense_arm_reaches_a_chunk_with_no_lexical_overlap(session: AsyncSession) -> None:
    user, kb, _ = await _corpus(session)

    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(2),  # matches "unrelated filler", which shares no word with the query
        filters=RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id]),
    )
    assert 2 in {hit.chunk_index for hit in hits}


async def test_a_chunk_found_by_both_arms_outranks_the_dense_arms_own_top_hit(
    session: AsyncSession,
) -> None:
    user, kb, _ = await _corpus(session)

    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(0),  # chunk 0 is the dense arm's rank-1 hit
        filters=RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id]),
    )
    assert hits[0].chunk_index == 1, "the chunk both arms returned should lead"
    assert hits[0].dense_rank is not None
    assert hits[0].sparse_rank == 1
    assert hits[0].score > hits[1].score


async def test_hit_carries_the_citation_payload(session: AsyncSession) -> None:
    """FR-CIT-03 renders from these, so T-305 must not need a second read to get them."""
    user, kb, doc = await _corpus(session)

    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(1),
        filters=RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id]),
    )
    hit = hits[0]
    assert hit.filename == "handbook.pdf"
    assert hit.document_id == doc.id
    assert hit.knowledge_base_id == kb.id
    assert RARE_TOKEN in hit.chunk_text


async def test_an_all_stopword_query_falls_back_to_dense_only(session: AsyncSession) -> None:
    """`websearch_to_tsquery` drops every lexeme, so the sparse arm contributes nothing."""
    user, kb, _ = await _corpus(session)

    hits = await HybridRetriever(session).search(
        "the of and to",
        _onehot(2),
        filters=RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id]),
    )
    assert hits, "dense results must survive a query with no usable lexemes"
    assert all(hit.sparse_rank is None for hit in hits)
    assert hits[0].chunk_index == 2


async def test_a_punctuation_only_query_short_circuits_the_sparse_arm(
    session: AsyncSession,
) -> None:
    user, kb, _ = await _corpus(session)

    hits = await HybridRetriever(session).search(
        '  -  ""  ',
        _onehot(0),
        filters=RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id]),
    )
    assert all(hit.sparse_rank is None for hit in hits)


# --- access control (FR-RET-04 / NFR-SEC-06) ----------------------------------
#
# One test per predicate, each asserting the row is *absent*. Every one of these is a
# document that is retrievable today if the corresponding predicate is dropped.


async def _search_all(session: AsyncSession, owner_id: uuid.UUID) -> list[int]:
    hits = await HybridRetriever(session).search(
        RARE_TOKEN, _onehot(1), filters=RetrievalFilter(owner_id=owner_id)
    )
    return [hit.chunk_index for hit in hits]


async def test_another_users_documents_are_invisible(session: AsyncSession) -> None:
    _, _, _ = await _corpus(session)
    intruder = await _make_user(session)
    assert await _search_all(session, intruder.id) == []


async def test_a_non_searchable_document_is_invisible(session: AsyncSession) -> None:
    """FR-ING-05: `searchable=false` is the synchronous deletion gate."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb, searchable=False)
    await _make_chunk(session, doc, kb, index=0, text_=RARE_TOKEN, embedding=_onehot(1))
    assert await _search_all(session, user.id) == []


async def test_a_soft_deleted_document_is_invisible(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb, deleted_at=datetime.now(UTC))
    await _make_chunk(session, doc, kb, index=0, text_=RARE_TOKEN, embedding=_onehot(1))
    assert await _search_all(session, user.id) == []


async def test_a_stale_document_version_is_invisible(session: AsyncSession) -> None:
    """R-36(4) should mean no such row exists; the predicate is what makes that true anyway."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb, current_version=2)
    await _make_chunk(
        session,
        doc,
        kb,
        index=0,
        text_=f"stale {RARE_TOKEN}",
        embedding=_onehot(1),
        document_version=1,
    )
    assert await _search_all(session, user.id) == []

    await _make_chunk(
        session,
        doc,
        kb,
        index=0,
        text_=f"current {RARE_TOKEN}",
        embedding=_onehot(1),
        document_version=2,
    )
    assert await _search_all(session, user.id) == [0]


async def test_another_tenants_chunks_are_invisible(session: AsyncSession) -> None:
    user, kb, _ = await _corpus(session)
    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(1),
        filters=RetrievalFilter(
            owner_id=user.id, tenant_id=uuid.uuid4(), knowledge_base_ids=[kb.id]
        ),
    )
    assert hits == []


async def test_document_id_scope_narrows_to_the_mentioned_documents(
    session: AsyncSession,
) -> None:
    """FR-ORC-06 `@`-mentions ride this filter."""
    user, kb, _ = await _corpus(session)
    other = await _make_document(session, user, kb)
    await _make_chunk(
        session, other, kb, index=0, text_=f"other doc {RARE_TOKEN}", embedding=_onehot(1)
    )

    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(1),
        filters=RetrievalFilter(owner_id=user.id, document_ids=[other.id]),
    )
    assert {hit.document_id for hit in hits} == {other.id}


# --- the FR-ORC-06 ambient scope (T-305, R-46(1)/(2)) -------------------------
#
# "The active chat's attachments + all GLOBAL documents visible to the user + any explicitly
# @-mentioned documents." The half that needs a database is the knowledge-base predicate:
# every test below passes with an owner-only filter, which is exactly why the bug it guards
# against — one chat answering from another chat's attachments — would have shipped silently.


async def _make_conversation(session: AsyncSession, owner):  # noqa: ANN001, ANN202
    conversation = Conversation(owner_id=owner.id, tenant_id=DEFAULT_TENANT_ID, title="chat")
    session.add(conversation)
    await session.flush()
    return conversation


async def _attach(session: AsyncSession, owner, conversation, text_: str):  # noqa: ANN001, ANN202
    """A document attached to one conversation, in that conversation's implicit KB (R-25)."""
    kb = await KnowledgeBaseRepository(session).get_or_create_for_conversation(
        conversation.id, owner_id=owner.id
    )
    doc = await _make_document(session, owner, kb)
    await _make_chunk(session, doc, kb, index=0, text_=text_, embedding=_onehot(1))
    return doc


async def _search_in(session: AsyncSession, owner, conversation, **overrides):  # noqa: ANN001, ANN003, ANN202
    hits = await HybridRetriever(session).search(
        RARE_TOKEN,
        _onehot(1),
        filters=RetrievalFilter(owner_id=owner.id, conversation_id=conversation.id, **overrides),
    )
    return {hit.document_id for hit in hits}


async def test_a_global_document_is_visible_from_every_conversation(
    session: AsyncSession,
) -> None:
    """FR-KBM-03's first half: GLOBAL documents are included in retrieval for every chat."""
    user, kb, _ = await _corpus(session)
    doc = await _make_document(session, user, kb)
    await _make_chunk(session, doc, kb, index=0, text_=f"global {RARE_TOKEN}", embedding=_onehot(1))

    first = await _make_conversation(session, user)
    second = await _make_conversation(session, user)
    assert doc.id in await _search_in(session, user, first)
    assert doc.id in await _search_in(session, user, second)


async def test_this_conversations_attachment_is_visible(session: AsyncSession) -> None:
    user = await _make_user(session)
    conversation = await _make_conversation(session, user)
    doc = await _attach(session, user, conversation, f"attached {RARE_TOKEN}")

    assert await _search_in(session, user, conversation) == {doc.id}


async def test_another_conversations_attachment_is_invisible(session: AsyncSession) -> None:
    """FR-KBM-03's second half, and the whole reason `conversation_id` exists on the filter.

    Both attachments belong to the same user, so `Document.owner_id` — the only scope this
    filter had before T-305 — admits both. Under R-25 every chat has its own knowledge base,
    so without the ambient predicate a user with fifty chats grounds every answer in all
    fifty, and the FR-CMP-06 footer count would be a lie about what was searched.
    """
    user = await _make_user(session)
    here = await _make_conversation(session, user)
    elsewhere = await _make_conversation(session, user)
    mine = await _attach(session, user, here, f"mine {RARE_TOKEN}")
    theirs = await _attach(session, user, elsewhere, f"theirs {RARE_TOKEN}")

    assert await _search_in(session, user, here) == {mine.id}
    assert await _search_in(session, user, elsewhere) == {theirs.id}


async def test_a_mention_narrows_within_the_ambient_scope(session: AsyncSession) -> None:
    """R-46(1): the mention filter is AND-ed with the scope, so it cannot widen it.

    A mention naming a document outside the caller's ambient scope retrieves **nothing**
    rather than reaching it — which is what makes "mentions narrow" safe to state without
    a second authorization check.
    """
    user = await _make_user(session)
    here = await _make_conversation(session, user)
    elsewhere = await _make_conversation(session, user)
    global_kb = await _make_kb(session, user)
    global_doc = await _make_document(session, user, global_kb)
    await _make_chunk(
        session, global_doc, global_kb, index=0, text_=f"global {RARE_TOKEN}", embedding=_onehot(1)
    )
    mine = await _attach(session, user, here, f"mine {RARE_TOKEN}")
    theirs = await _attach(session, user, elsewhere, f"theirs {RARE_TOKEN}")

    # In scope and mentioned: exactly that document, not the ambient set around it.
    assert await _search_in(session, user, here, document_ids=[mine.id]) == {mine.id}
    assert await _search_in(session, user, here, document_ids=[global_doc.id]) == {global_doc.id}
    # Out of scope and mentioned: nothing.
    assert await _search_in(session, user, here, document_ids=[theirs.id]) == set()


async def test_the_ambient_scope_never_reaches_another_users_documents(
    session: AsyncSession,
) -> None:
    """The knowledge-base predicate is owner-scoped too — belt and braces with `Document`.

    It matters because the two are separable: a future feature that shares a conversation
    would otherwise make the KB half admit rows the document half still refuses, and the
    order in which those two facts change is not something to leave to chance.
    """
    user = await _make_user(session)
    intruder = await _make_user(session)
    conversation = await _make_conversation(session, intruder)
    await _attach(session, intruder, conversation, f"not yours {RARE_TOKEN}")

    assert await _search_in(session, user, conversation) == set()


async def test_a_conversation_with_no_attachments_still_sees_the_global_kb(
    session: AsyncSession,
) -> None:
    """The per-chat KB is materialised on first upload (R-25), so usually it does not exist."""
    user, kb, doc = await _corpus(session)
    conversation = await _make_conversation(session, user)

    assert await _search_in(session, user, conversation) == {doc.id}


# --- overlap dedupe end to end (R-35(5) / R-37(6)) ----------------------------


async def test_overlapping_neighbours_are_deduped_in_a_real_search(
    session: AsyncSession,
) -> None:
    """Both chunks carry the shared text, so both match; only one should come back."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    shared = f"the {RARE_TOKEN} fault clears after a controller reset"
    await _make_chunk(
        session,
        doc,
        kb,
        index=0,
        text_=f"preamble {shared}",
        embedding=_onehot(1),
        block_order=0,
        block_chunk_index=0,
    )
    await _make_chunk(
        session,
        doc,
        kb,
        index=1,
        text_=f"{shared} and then continues",
        embedding=_onehot(1),
        block_order=0,
        block_chunk_index=1,
    )
    filters = RetrievalFilter(owner_id=user.id, knowledge_base_ids=[kb.id])

    hits = await HybridRetriever(session).search(RARE_TOKEN, _onehot(1), filters=filters)
    assert len(hits) == 1

    kept_both = await HybridRetriever(session, _settings(dedupe_adjacent=False)).search(
        RARE_TOKEN, _onehot(1), filters=filters
    )
    assert len(kept_both) == 2


# --- index usage --------------------------------------------------------------
#
# The regression guard for the bug this task fixed: the dense arm used to order on the bare
# `vector` operator while the index is built on the `halfvec` cast, so every "dense" search
# was a sequential scan. Both indexes are functional, so this asserts on the expressions the
# code emits rather than on a whole join plan the planner is free to reshape.


async def _explain(session: AsyncSession, stmt) -> str:  # noqa: ANN001
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).all()
    return "\n".join(row[0] for row in rows)


async def test_dense_expression_matches_the_hnsw_index(session: AsyncSession) -> None:
    await _corpus(session)
    distance = dense_distance(_onehot(1))
    plan = await _explain(session, select(DocumentChunk.id).order_by(distance).limit(5))
    assert "ix_document_chunks_embedding" in plan, plan


async def test_fts_expression_matches_the_gin_index(session: AsyncSession) -> None:
    await _corpus(session)
    settings = _settings()
    document = fts_document(settings)
    tsquery = fts_query(websearch_input(RARE_TOKEN), settings)
    plan = await _explain(session, select(DocumentChunk.id).where(document.bool_op("@@")(tsquery)))
    assert "ix_document_chunks_chunk_text_fts" in plan, plan


# --- query preparation --------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("what is the retention policy", "what OR is OR the OR retention OR policy"),
        ("  spaced   out  ", "spaced OR out"),
        ("-negated term", "negated OR term"),  # a bare `-` would become NOT
        ('"phrase" term', "phrase OR term"),  # a bare `"` would open a phrase
        ("", ""),
        ('- ""', ""),
    ],
)
def test_websearch_input_ors_terms_and_strips_meaning_changing_operators(
    query: str, expected: str
) -> None:
    assert websearch_input(query) == expected


# --- read-back by id (T-306) --------------------------------------------------


async def test_fetch_chunks_returns_rows_in_the_requested_order(session: AsyncSession) -> None:
    """The requested order is the R-46(3) merged ranking, and `IN` guarantees no order at all."""
    user, kb, doc = await _corpus(session)
    stmt = select(DocumentChunk).order_by(DocumentChunk.chunk_index)
    ids = [chunk.id for chunk in (await session.execute(stmt)).scalars().all()]
    requested = [ids[2], ids[0], ids[1]]

    hits = await fetch_chunks(session, requested, filters=RetrievalFilter(owner_id=user.id))

    assert [hit.chunk_id for hit in hits] == requested
    assert [hit.chunk_index for hit in hits] == [2, 0, 1]


async def test_fetch_chunks_carries_the_text_and_the_citation_payload(
    session: AsyncSession,
) -> None:
    """What the reranker and T-307's composer need — `RAGState` carries none of it."""
    user, kb, doc = await _corpus(session)
    chunk = await _make_chunk(
        session,
        doc,
        kb,
        index=9,
        text_="the refund window is 30 days",
        embedding=_onehot(9),
        block_order=1,
        block_chunk_index=0,
    )

    (hit,) = await fetch_chunks(session, [chunk.id], filters=RetrievalFilter(owner_id=user.id))

    assert hit.chunk_text == "the refund window is 30 days"
    assert hit.filename == "handbook.pdf"
    assert hit.document_id == doc.id
    assert hit.block_order == 1


async def test_fetch_chunks_applies_the_same_access_filters(session: AsyncSession) -> None:
    """The point of building it on `_access_predicates`: a checkpointed id is not a capability.

    The ids reach this function out of a checkpoint that may predate a deletion, a role change
    or a replace. Reading them back under a weaker predicate set would make the checkpoint a
    way to read rows the live scope no longer contains (R-42(3) at the row level).
    """
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    chunk = await _make_chunk(session, doc, kb, index=0, text_="secret", embedding=_onehot(0))
    intruder = await _make_user(session)

    assert await fetch_chunks(session, [chunk.id], filters=RetrievalFilter(owner_id=user.id))
    assert (
        await fetch_chunks(session, [chunk.id], filters=RetrievalFilter(owner_id=intruder.id)) == []
    )


async def test_fetch_chunks_omits_a_document_soft_deleted_since_retrieval(
    session: AsyncSession,
) -> None:
    """FR-CIT-06(1)/(5) discharged one stage before the citation exists."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    chunk = await _make_chunk(session, doc, kb, index=0, text_="gone", embedding=_onehot(0))

    doc.deleted_at = datetime.now(UTC)
    await session.flush()

    assert await fetch_chunks(session, [chunk.id], filters=RetrievalFilter(owner_id=user.id)) == []


async def test_fetch_chunks_short_circuits_on_an_empty_id_list(session: AsyncSession) -> None:
    assert await fetch_chunks(session, [], filters=RetrievalFilter(owner_id=uuid.uuid4())) == []
