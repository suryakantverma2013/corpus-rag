"""Repository-layer unit tests (T-102).

Exercise each repository against the real (transactional, rolled-back) DB, plus the
pgvector retriever behind the OI-18 interface. Focus is on the spec-load-bearing
behaviors: per-KB checksum dedup (FR-KBM-08), incremental chunk hashing (FR-ING-03),
job idempotency (FR-ING-04), sidebar ordering (FR-SBR-03), and in-query access
filtering (FR-RET-04).
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import (
    AuditEventType,
    DocumentStatus,
    Feedback,
    JobStatus,
    JobType,
    KBVisibility,
    MessageRole,
)
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.models.message import Message
from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.chunks import DocumentChunkRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.jobs import KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.retrieval import PgVectorRetriever
from app.db.repositories.users import UserRepository
from app.rag.retrieval import RetrievalFilter, Retriever

EMBEDDING_DIM = 3072


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _onehot(index: int) -> list[float]:
    """A distinct 3072-dim unit vector (cosine sim 1 with itself, 0 with the others)."""
    vec = [0.0] * EMBEDDING_DIM
    vec[index] = 1.0
    return vec


async def _make_user(session: AsyncSession, email: str | None = None):
    return await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=email or f"{uuid.uuid4().hex}@example.com"
    )


async def _make_kb(session: AsyncSession, owner):
    return await KnowledgeBaseRepository(session).get_or_create_default(owner.id)


async def _make_document(
    session: AsyncSession,
    owner,
    kb,
    *,
    checksum: str | None = None,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    searchable: bool = True,
) -> Document:
    doc = Document(
        owner_id=owner.id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="file.pdf",
        storage_uri="s3://corpus/file.pdf",
        checksum_sha256=checksum or _sha(uuid.uuid4().hex),
        status=status,
        searchable=searchable,
    )
    return await DocumentRepository(session).add(doc)


async def _make_chunk(
    session: AsyncSession, doc, kb, *, index: int, embedding=None, is_active: bool = True
) -> DocumentChunk:
    chunk = DocumentChunk(
        document_id=doc.id,
        document_version=1,
        chunk_index=index,
        chunk_hash=_sha(f"{doc.id}:{index}"),
        embedding_fingerprint=_sha(f"fp:{doc.id}:{index}"),
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        chunk_text=f"chunk {index}",
        embedding=embedding,
        is_active=is_active,
    )
    session.add(chunk)
    await session.flush()
    return chunk


# --- users ---------------------------------------------------------------


async def test_user_upsert_from_claims_creates_then_updates(session: AsyncSession) -> None:
    repo = UserRepository(session)
    sub = uuid.uuid4()
    created = await repo.upsert_from_claims(sub=sub, email="a@example.com", display_name="A")
    assert created.id == sub
    # Second call with the same sub updates in place (no duplicate row).
    updated = await repo.upsert_from_claims(sub=sub, email="a2@example.com")
    assert updated.id == sub
    assert updated.email == "a2@example.com"
    assert await repo.get_by_email("a2@example.com") is not None


# --- knowledge bases -----------------------------------------------------


async def test_default_kb_is_idempotent(session: AsyncSession) -> None:
    user = await _make_user(session)
    repo = KnowledgeBaseRepository(session)
    first = await repo.get_or_create_default(user.id)
    second = await repo.get_or_create_default(user.id)
    assert first.id == second.id
    assert len(await repo.list_by_owner(user.id)) == 1


# --- documents -----------------------------------------------------------


async def test_checksum_dedup_scoped_per_kb(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb_repo = KnowledgeBaseRepository(session)
    kb1 = await kb_repo.get_or_create_default(user.id, name="KB1")
    # A second, per-conversation KB for the same owner.
    kb2 = await kb_repo.add(
        KnowledgeBase(
            owner_id=user.id,
            tenant_id=DEFAULT_TENANT_ID,
            name="KB2",
            visibility=KBVisibility.CONVERSATION,
        )
    )
    checksum = _sha("same-bytes")
    await _make_document(session, user, kb1, checksum=checksum)
    repo = DocumentRepository(session)

    # Same checksum, same KB → duplicate found.
    assert await repo.find_by_checksum(knowledge_base_id=kb1.id, checksum_sha256=checksum)
    # Same checksum, different KB → not a duplicate (FR-KBM-08 is per-KB).
    assert await repo.find_by_checksum(knowledge_base_id=kb2.id, checksum_sha256=checksum) is None


async def test_mark_delete_pending_clears_searchable(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb, searchable=True)
    repo = DocumentRepository(session)
    await repo.mark_delete_pending(doc)
    assert doc.status is DocumentStatus.DELETE_PENDING
    assert doc.searchable is False


# --- chunks --------------------------------------------------------------


async def test_deactivate_soft_deletes_chunks(session: AsyncSession) -> None:
    """`is_active` means FR-ING-05 soft-delete only — never version management (R-36(7))."""
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    repo = DocumentChunkRepository(session)
    c0 = await _make_chunk(session, doc, kb, index=0)
    c1 = await _make_chunk(session, doc, kb, index=1)

    assert {chunk.id for chunk in await repo.list_by_document(doc.id)} == {c0.id, c1.id}
    assert await repo.deactivate([c0.id]) == 1
    assert {chunk.id for chunk in await repo.list_by_document(doc.id)} == {c1.id}


# --- jobs ----------------------------------------------------------------


async def test_job_idempotency_and_status_timestamps(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    repo = KnowledgeJobRepository(session)
    job = await repo.add(
        KnowledgeJob(
            document_id=doc.id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            idempotency_key="ingest:abc",
        )
    )
    assert (await repo.get_by_idempotency_key("ingest:abc")).id == job.id

    await repo.update_status(job, JobStatus.RUNNING)
    assert job.started_at is not None and job.completed_at is None
    await repo.update_status(job, JobStatus.SUCCEEDED, progress=100)
    assert job.completed_at is not None and job.progress == 100


# --- conversations & messages -------------------------------------------


async def test_conversation_ordering_and_rename(session: AsyncSession) -> None:
    user = await _make_user(session)
    repo = ConversationRepository(session)
    a = await repo.add(Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="A"))
    b = await repo.add(Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="B"))
    # Touch A so it becomes most-recently-updated.
    await repo.rename(a, "A renamed")
    listed = await repo.list_by_owner(user.id)
    assert [c.id for c in listed][0] == a.id
    assert {c.id for c in listed} == {a.id, b.id}
    assert a.title == "A renamed"


async def test_message_feedback_and_evaluation(session: AsyncSession) -> None:
    user = await _make_user(session)
    conv = await ConversationRepository(session).add(
        Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="chat")
    )
    repo = MessageRepository(session)
    await repo.add(Message(conversation_id=conv.id, role=MessageRole.USER, content="hi"))
    ai = await repo.add(Message(conversation_id=conv.id, role=MessageRole.AI, content="hello"))
    listed = await repo.list_by_conversation(conv.id)
    assert [m.role for m in listed] == [MessageRole.USER, MessageRole.AI]

    await repo.set_feedback(ai, Feedback.UP)
    await repo.set_evaluation(ai, {"relevancy": 0.9, "faithfulness": 0.95})
    assert ai.feedback is Feedback.UP
    assert ai.evaluation["faithfulness"] == 0.95


# --- retrieval (OI-18 interface) ----------------------------------------


async def test_pgvector_retriever_conforms_to_protocol(session: AsyncSession) -> None:
    assert isinstance(PgVectorRetriever(session), Retriever)


async def test_dense_search_ranks_and_filters(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    await _make_chunk(session, doc, kb, index=0, embedding=_onehot(0))
    await _make_chunk(session, doc, kb, index=1, embedding=_onehot(1))
    await _make_chunk(session, doc, kb, index=2, embedding=_onehot(2))

    retriever = PgVectorRetriever(session)
    hits = await retriever.search(
        _onehot(1), filters=RetrievalFilter(knowledge_base_ids=[kb.id]), top_k=3
    )
    assert hits, "expected results"
    # Nearest is the matching one-hot vector, with cosine similarity ~1.0.
    assert hits[0].chunk_index == 1
    assert hits[0].score > 0.99

    # A different KB scope returns nothing (in-query access filter, FR-RET-04).
    other = await retriever.search(
        _onehot(1), filters=RetrievalFilter(knowledge_base_ids=[uuid.uuid4()]), top_k=3
    )
    assert other == []


async def test_dense_search_excludes_inactive(session: AsyncSession) -> None:
    user = await _make_user(session)
    kb = await _make_kb(session, user)
    doc = await _make_document(session, user, kb)
    await _make_chunk(session, doc, kb, index=0, embedding=_onehot(0), is_active=False)

    hits = await PgVectorRetriever(session).search(
        _onehot(0), filters=RetrievalFilter(knowledge_base_ids=[kb.id]), top_k=3
    )
    assert hits == []


# --- audit log (T-107) ---------------------------------------------------


async def test_audit_log_record_and_filtered_list(session: AsyncSession) -> None:
    actor = await _make_user(session)
    other = await _make_user(session)
    repo = AuditLogRepository(session)

    await repo.record(
        event_type=AuditEventType.AUTH, actor_id=actor.id, details={"action": "login"}
    )
    await repo.record(
        event_type=AuditEventType.USER_ROLE_CHANGE,
        actor_id=actor.id,
        target_id=str(other.id),
        details={"action": "create"},
    )
    # A pre-auth failed login (actor_id null).
    await repo.record(event_type=AuditEventType.AUTH, details={"action": "login_failed"})

    # No filter → all three, newest-first (id tiebreak keeps it deterministic).
    assert len(await repo.list_events()) == 3
    # Filter by event type.
    auth_rows = await repo.list_events(event_type=AuditEventType.AUTH)
    assert len(auth_rows) == 2
    assert all(r.event_type is AuditEventType.AUTH for r in auth_rows)
    # Filter by actor (excludes the null-actor row).
    actor_rows = await repo.list_events(actor_id=actor.id)
    assert len(actor_rows) == 2
    assert all(r.actor_id == actor.id for r in actor_rows)
    # Paging.
    assert len(await repo.list_events(limit=1)) == 1
    assert len(await repo.list_events(limit=1, offset=2)) == 1
