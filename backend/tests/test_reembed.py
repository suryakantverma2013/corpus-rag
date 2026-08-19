"""The FR-ING-03 re-embed trigger (T-608, R-84).

Same harness as `test_replace.py` — `LocalFilesystemStorage` under `tmp_path`, a recording fake
queue — because a rebuild *is* a replace with the document's own bytes, and everything here is
the synchronous half. What the worker then does with the v(n+1) job lives in
`test_ingest_task.py`; the route -> worker -> route round trip is `tests/scenarios/test_scope.py`.

Two tests carry more weight than the rest:

* `test_a_rebuild_copies_the_original_forward` — without the copy the worker's
  `_purge_superseded_versions(range(n, n+1))` deletes the prefix holding the live
  `storage_uri`'s bytes after the swap, destroying the only original of a document nothing can
  then rebuild. Skipping the copy looks like an optimisation and is data loss, and it is
  invisible to every other assertion here.
* `test_a_document_built_by_the_configured_pipeline_is_refused` — R-84(3). It is the whole
  difference between this trigger and widening `/retry` to `ACTIVE` documents: without it the
  route is a re-embed button that charges full price for a byte-identical result.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tools import reembed as reembed_cli

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import AuditEventType, DocumentStatus, JobStatus, JobType
from app.db.models.audit_log import AuditLog
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.chunks import DocumentChunkRepository
from app.db.repositories.jobs import ENQUEUE_FAILED, SUPERSEDED
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.retrieval import HybridRetriever
from app.db.repositories.users import UserRepository
from app.rag.retrieval import RetrievalFilter
from app.services.documents import (
    DocumentNotFoundError,
    NotRebuildableError,
    NotStaleError,
    OriginalCorruptError,
    rebuild_document,
)
from app.services.embeddings import FakeEmbeddingClient, build_embedding_client
from app.services.jobs import JobQueueError, get_job_queue
from app.services.model_selection import (
    ModelSlot,
    clear_model_override,
    set_model_override,
)
from app.services.object_storage import (
    LocalFilesystemStorage,
    ObjectStorageError,
    get_object_storage,
    original_key,
)
from app.services.reembed import configured_pipeline, is_stale, plan_reembed

pytestmark = pytest.mark.usefixtures("patch_jwks")

_TEXT = "The perihelion precession of Mercury is a rare lexical anchor for this fixture."
_PAYLOAD = b"%PDF-1.7\noriginal bytes\n%%EOF\n"
_PDF_HEAD = b"%PDF-1.7\n"
_PDF_TAIL = b"\n%%EOF\n"


# ---- fixtures ----


class _RecordingQueue:
    def __init__(self) -> None:
        self.ingests: list[dict[str, object]] = []
        self.fail = False

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        if self.fail:
            raise JobQueueError("broker down")
        self.ingests.append(
            {"job_id": job_id, "document_id": document_id, "idempotency_key": idempotency_key}
        )

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:  # pragma: no cover — a rebuild never enqueues a deletion
        raise AssertionError("a rebuild must never enqueue a deletion")

    async def aclose(self) -> None:
        return None


class _CountingStorage:
    """`LocalFilesystemStorage` that counts reads and can act during one.

    Two jobs, both forced by mutation testing. **Counting `get`** is how the pre-download gates
    are told apart from the ones under the row lock: `rebuild_document` checks the state and
    the staleness *twice*, and a mutation removing either of the first pair leaves the refusal
    intact — the second pair catches it — so the only observable difference is whether 50 MB
    was fetched first. R-63(6)'s shape: **a check that must happen before a download is
    asserted by the absence of the download.**

    **`on_get`** is how the second pair is reached at all: it runs while the download is in
    flight, which is the only window in which a concurrent replace, delete or rebuild can land.
    """

    def __init__(self, inner: LocalFilesystemStorage) -> None:
        self._inner = inner
        self.gets = 0
        self.puts: list[str] = []
        self.on_get: object | None = None

    async def get(self, key: str) -> bytes:
        self.gets += 1
        payload = await self._inner.get(key)
        if self.on_get is not None:
            await self.on_get()  # type: ignore[operator]
        return payload

    async def put(self, key: str, data, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.puts.append(key)
        return await self._inner.put(key, data, **kwargs)

    def __getattr__(self, name: str):  # noqa: ANN202 — everything else delegates unchanged
        return getattr(self._inner, name)


@pytest.fixture
def storage(tmp_path) -> _CountingStorage:  # noqa: ANN001
    return _CountingStorage(LocalFilesystemStorage(tmp_path / "objects"))


@pytest.fixture
def queue() -> _RecordingQueue:
    return _RecordingQueue()


@pytest.fixture
def app(app, storage: _CountingStorage, queue: _RecordingQueue):  # noqa: ANN001, ANN201
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _owner(session: AsyncSession) -> uuid.UUID:
    """A real `users` row, because `knowledge_bases.owner_id` has a foreign key to it."""
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=sub, email=f"{sub.hex[:8]}@corpus.local", display_name="Owner"
    )
    return sub


def _provenance(**overrides: str) -> dict:
    """The `document_chunks.metadata` provenance a fresh chunk carries, with drift injected.

    Built from `configured_pipeline()` rather than from literals: the fixture has to agree with
    whatever `OPENAI_EMBEDDING_MODEL` and the `CHUNKER_*` knobs say in this environment, or
    every "fresh" document in this module would read as stale for the wrong reason.
    """
    pipeline = configured_pipeline()
    meta = {
        "embedding_model": pipeline.embedding_model,
        "chunking_version": pipeline.chunking_version,
        "preprocessing_version": pipeline.preprocessing_version,
    }
    meta.update(overrides)
    return meta


async def _seed_document(
    session: AsyncSession,
    storage: _CountingStorage,
    *,
    owner_id: uuid.UUID,
    payload: bytes | None = None,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    current_version: int = 1,
    provenance: dict | None = None,
    chunk_versions: tuple[int, ...] | None = None,
    token_count: int = 20,
    deleted_at=None,  # noqa: ANN001
    store_object: bool = True,
) -> Document:
    """An `ACTIVE` document with its stored original and one embedded chunk per version.

    The payload is unique per call unless the caller pins it: the bytes are hashed into
    `checksum_sha256`, which is UNIQUE per knowledge base while `deleted_at IS NULL`, so two
    identical fixtures in one owner's KB are a constraint violation rather than two documents.
    """
    document_id = uuid.uuid4()
    if payload is None:
        payload = _PDF_HEAD + document_id.hex.encode() + _PDF_TAIL
    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    key = original_key(
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=kb.id,
        document_id=document_id,
        version=current_version,
        filename="handbook.pdf",
    )
    uri = f"file://{key}"
    if store_object:
        stored = await storage.put(key, payload)
        uri = stored.uri

    document = Document(
        id=document_id,
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        mime_type="application/pdf",
        storage_uri=uri,
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        status=status,
        current_version=current_version,
        searchable=status is DocumentStatus.ACTIVE,
        page_count=58,
        chunk_count=1,
        deleted_at=deleted_at,
    )
    session.add(document)
    await session.flush()

    vector = await FakeEmbeddingClient().embed_query(_TEXT)
    for version in chunk_versions or (current_version,):
        session.add(
            DocumentChunk(
                document_id=document_id,
                document_version=version,
                chunk_index=0,
                chunk_hash=uuid.uuid4().hex * 2,
                embedding_fingerprint=uuid.uuid4().hex * 2,
                token_count=token_count,
                tenant_id=DEFAULT_TENANT_ID,
                knowledge_base_id=kb.id,
                chunk_text=_TEXT,
                embedding=vector,
                meta={
                    "block_order": 0,
                    "block_chunk_index": 0,
                    **(provenance if provenance is not None else _provenance()),
                },
            )
        )
    await session.flush()
    return document


async def _stale(session: AsyncSession, owner_id: uuid.UUID | None = None) -> list:
    plan = await plan_reembed(session, owner_id=owner_id)
    return list(plan.documents)


async def _jobs_for(session: AsyncSession, document_id: uuid.UUID) -> list[KnowledgeJob]:
    stmt = select(KnowledgeJob).where(KnowledgeJob.document_id == document_id)
    return list((await session.scalars(stmt)).all())


async def _search(session: AsyncSession, owner_id: uuid.UUID) -> list:
    embedder = FakeEmbeddingClient()
    return await HybridRetriever(session).search(
        "perihelion precession of Mercury",
        await embedder.embed_query("perihelion precession of Mercury"),
        filters=RetrievalFilter(owner_id=owner_id),
    )


def _version_key(document: Document, *, version: int, filename: str = "original.pdf") -> str:
    return (
        f"tenants/{document.tenant_id}/kb/{document.knowledge_base_id}"
        f"/documents/{document.id}/v{version}/{filename}"
    )


# ---- 1. the configured pipeline -----------------------------------------------


@pytest.mark.parametrize("backend", ["openai", "fake"])
def test_the_reported_model_is_the_one_the_worker_embeds_with(backend: str) -> None:
    """`configured_pipeline` reads a setting; the worker reads a client. Pin the equivalence.

    `chunk_document(parsed, embedding_model=deps.embedder.model)` is what reaches
    `compute_embedding_fingerprint`, so if these two ever diverged the whole tool would lie in
    the most confusing possible way — reporting drift that does not exist, or missing drift that
    does — while every individual component still passed its own tests.

    Both backends, because `build_embedding_client` branches on `EMBEDDING_BACKEND` and only one
    of the two branches runs in CI. The fake is what ~1,800 tests embed with; the real client is
    what production fingerprints with, and it is the one nobody would notice going wrong here.
    """
    from app.config import get_settings

    settings = get_settings().model_copy(deep=True)
    settings.embedding.backend = backend  # type: ignore[assignment]
    assert build_embedding_client(settings).model == configured_pipeline(settings).embedding_model


def test_the_chunking_version_is_the_composite_not_the_bare_constant() -> None:
    """R-35(8): the sizing knobs are folded in, so a `CHUNKER_*` retune registers as drift."""
    assert "/" in configured_pipeline().chunking_version


# ---- 2. what counts as stale -------------------------------------------------


async def test_a_document_built_by_the_configured_pipeline_is_not_listed(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    owner = await _owner(session)
    await _seed_document(session, storage, owner_id=owner)
    assert await _stale(session, owner) == []


async def test_staleness_follows_the_operators_slot_not_the_environment(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """T-612/R-87(2). The report must ask the same question the worker will answer.

    `configured_pipeline` used to read `OPENAI_EMBEDDING_MODEL` unconditionally, which was
    correct while nothing could move it. With a slot in play that reading is a silent lie in
    both directions: with an override set, a freshly-ingested document reads as stale forever
    (it was built with the override, compared against the environment), and the documents that
    genuinely *are* in the old space read as fresh. Nothing fails — the comparison is one SQL
    predicate over recorded provenance, so it simply answers the wrong question.
    """
    from app.config import get_settings

    owner = await _owner(session)
    configured = get_settings().openai.embedding_model
    document = await _seed_document(session, storage, owner_id=owner)  # the environment default
    assert await _stale(session, owner) == [], "the fixture is not stale before the flip"

    await set_model_override(
        session, slot=ModelSlot.EMBEDDING, model_id="operators-choice", updated_by="test"
    )
    await session.flush()

    plan = await plan_reembed(session, owner_id=owner)

    assert plan.pipeline.embedding_model == "operators-choice", (
        "the report still describes the environment default, so it is measuring drift "
        "against a model nothing will embed with"
    )
    assert [row.document_id for row in plan.documents] == [document.id], (
        "the document built with the previous model is the one now in the old vector space"
    )
    assert plan.documents[0].embedding_model_drift is True
    assert configured != "operators-choice", "the fixture would prove nothing otherwise"


async def test_clearing_the_slot_makes_the_old_corpus_fresh_again(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """The inverse, which is what makes the flip reversible without a rebuild.

    An operator who moves the slot, sees the price and changes their mind must be able to
    `clear` it and find the corpus fresh — otherwise the report would keep demanding a rebuild
    into a model nobody is using any more.
    """
    owner = await _owner(session)
    await _seed_document(session, storage, owner_id=owner)
    await set_model_override(
        session, slot=ModelSlot.EMBEDDING, model_id="operators-choice", updated_by="test"
    )
    await session.flush()
    assert await _stale(session, owner) != []

    await clear_model_override(session, slot=ModelSlot.EMBEDDING)
    await session.flush()

    assert await _stale(session, owner) == []


@pytest.mark.parametrize(
    "drifted",
    ["embedding_model", "chunking_version", "preprocessing_version"],
)
async def test_each_fingerprint_input_is_a_drift_signal(
    session: AsyncSession, storage: _CountingStorage, drifted: str
) -> None:
    """All three non-text inputs, not only the model.

    FR-ING-03 names the embedding model, the chunking strategy *and* the preprocessing
    strategy, and R-34(6) says in as many words that bumping `PREPROCESSING_VERSION` "forces
    re-embedding of the affected chunks" — which nothing could act on either until now.
    """
    owner = await _owner(session)
    await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(**{drifted: "something-else"})
    )

    rows = await _stale(session, owner)
    assert len(rows) == 1
    assert rows[0].drifted_inputs == (drifted,)


async def test_a_chunk_with_no_recorded_provenance_is_stale(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """The `IS DISTINCT FROM` half.

    `document_chunks.metadata` defaults to `'{}'`, so a row written before R-35's payload
    became a contract yields SQL NULL for every key — and `NULL != 'x'` is NULL, neither true
    nor false. Under a `!=` predicate the oldest rows in the corpus would read as fresh, which
    is the one direction of this check that must never fail silently.
    """
    owner = await _owner(session)
    await _seed_document(session, storage, owner_id=owner, provenance={})

    rows = await _stale(session, owner)
    assert len(rows) == 1
    assert rows[0].drifted_inputs == (
        "embedding_model",
        "chunking_version",
        "preprocessing_version",
    )


async def test_only_the_live_version_is_examined(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """A superseded version's provenance is not the corpus's.

    Joined on `document_version = documents.current_version`, so stale rows a crashed swap
    left behind at v1 cannot make a healthy v2 document look like work.
    """
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, current_version=2, chunk_versions=(2,)
    )
    kb_id = document.knowledge_base_id
    session.add(
        DocumentChunk(
            document_id=document.id,
            document_version=1,
            chunk_index=0,
            chunk_hash=uuid.uuid4().hex * 2,
            embedding_fingerprint=uuid.uuid4().hex * 2,
            token_count=20,
            tenant_id=DEFAULT_TENANT_ID,
            knowledge_base_id=kb_id,
            chunk_text=_TEXT,
            meta=_provenance(embedding_model="ancient-model"),
        )
    )
    await session.flush()

    assert await _stale(session, owner) == []


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.FAILED,
        DocumentStatus.QUEUED,
        DocumentStatus.EMBEDDING,
        DocumentStatus.DELETE_PENDING,
    ],
)
async def test_only_active_documents_are_listed(
    session: AsyncSession, storage: _CountingStorage, status: DocumentStatus
) -> None:
    """R-84(4). `FAILED` is the interesting one: `/retry` already rebuilds it."""
    owner = await _owner(session)
    await _seed_document(
        session,
        storage,
        owner_id=owner,
        status=status,
        provenance=_provenance(embedding_model="old"),
    )
    assert await _stale(session, owner) == []


async def test_a_deleted_document_is_not_listed(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    owner = await _owner(session)
    from datetime import UTC, datetime

    await _seed_document(
        session,
        storage,
        owner_id=owner,
        status=DocumentStatus.DELETED,
        deleted_at=datetime.now(UTC),
        provenance=_provenance(embedding_model="old"),
    )
    assert await _stale(session, owner) == []


async def test_a_document_with_an_open_ingest_job_is_in_flight_not_work(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """The `NOT EXISTS` clause, and it is the whole of R-84(9)'s resumability.

    Without it every run re-queues the batch the previous run is still rebuilding — and each
    re-queue burns another version, so a tool meant to converge would instead walk the version
    counter upwards for as long as an operator kept typing.
    """
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    assert len(await _stale(session, owner)) == 1

    session.add(
        KnowledgeJob(
            document_id=document.id,
            job_type=JobType.INGEST,
            status=JobStatus.QUEUED,
            document_version=2,
            idempotency_key=f"ingest:{document.id}:v2",
        )
    )
    await session.flush()

    plan = await plan_reembed(session, owner_id=owner)
    assert plan.documents == ()
    assert plan.totals.documents == 0
    assert plan.totals.in_flight == 1, "reported, not silently dropped"


async def test_the_owner_filter_narrows_the_set(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    mine, theirs = await _owner(session), await _owner(session)
    stale = _provenance(embedding_model="old")
    await _seed_document(session, storage, owner_id=mine, provenance=stale)
    await _seed_document(session, storage, owner_id=theirs, provenance=stale)

    assert len(await _stale(session, mine)) == 1
    assert len(await _stale(session, theirs)) == 1


async def test_totals_cover_the_whole_set_while_documents_are_one_page(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """R-80(4)'s shape: the decision is about the total, the action about the page."""
    owner = await _owner(session)
    stale = _provenance(embedding_model="old")
    for _ in range(3):
        await _seed_document(session, storage, owner_id=owner, provenance=stale, token_count=100)

    plan = await plan_reembed(session, owner_id=owner, limit=2)
    assert len(plan.documents) == 2
    assert plan.totals.documents == 3
    assert plan.totals.chunks == 3
    assert plan.totals.token_count == 300, "the cost of the whole run, not of the page"


async def test_paging_is_deterministic(session: AsyncSession, storage: _CountingStorage) -> None:
    """T-108's lesson: timestamps tie, so the id breaks it or a bounded run is not reproducible."""
    owner = await _owner(session)
    stale = _provenance(embedding_model="old")
    for _ in range(4):
        await _seed_document(session, storage, owner_id=owner, provenance=stale)

    whole = await plan_reembed(session, owner_id=owner, limit=4)
    one_at_a_time = [
        row.document_id
        for offset in range(4)
        for row in (await plan_reembed(session, owner_id=owner, limit=1, offset=offset)).documents
    ]
    assert [row.document_id for row in whole.documents] == one_at_a_time
    assert len(set(one_at_a_time)) == 4, "no row may be skipped or served twice across pages"


async def test_the_rebuild_precondition_follows_the_slot_too(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """`is_stale` is R-84(3)'s precondition, and it has to answer the same question the report does.

    `rebuild_document` refuses with `NOT_STALE` when this returns false. Reading the
    environment default here while the report reads the slot would produce the worst possible
    pairing: `GET /admin/documents/stale` lists a document, and `POST .../reembed` on that
    same document answers 409 — an operator staring at two endpoints that disagree, with
    neither of them wrong about anything it can see.

    Found by mutation: pointing this one function back at `configured_pipeline` left the whole
    suite green, because every other assertion goes through `plan_reembed`.
    """
    owner = await _owner(session)
    document = await _seed_document(session, storage, owner_id=owner)
    assert await is_stale(session, document.id) is False

    await set_model_override(
        session, slot=ModelSlot.EMBEDDING, model_id="operators-choice", updated_by="test"
    )
    await session.flush()

    assert await is_stale(session, document.id) is True


async def test_is_pipeline_stale_agrees_with_the_enumeration(
    session: AsyncSession, storage: _CountingStorage
) -> None:
    """One predicate, two readers. A route that refused documents the report listed — or
    accepted ones it did not — would be the more confusing of the two failures."""
    owner = await _owner(session)
    fresh = await _seed_document(session, storage, owner_id=owner)
    stale = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(chunking_version="9/9/9/9/9")
    )
    pipeline = configured_pipeline()
    repo = DocumentChunkRepository(session)
    inputs = {
        "embedding_model": pipeline.embedding_model,
        "chunking_version": pipeline.chunking_version,
        "preprocessing_version": pipeline.preprocessing_version,
    }

    assert await repo.is_pipeline_stale(stale.id, **inputs) is True
    assert await repo.is_pipeline_stale(fresh.id, **inputs) is False
    assert await repo.is_pipeline_stale(uuid.uuid4(), **inputs) is False


# ---- 3. the rebuild ----------------------------------------------------------


async def test_a_rebuild_copies_the_original_forward(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """The load-bearing test, and the one no other assertion here can stand in for.

    After the swap the worker runs `_purge_superseded_versions(first=previous, last=target)` —
    `range(n, n+1)` — so if the rebuild did not write a v(n+1) object the purge would delete
    the prefix holding the live `storage_uri`'s bytes, and nothing could rebuild the document
    again. Every other assertion in this module passes without the copy.
    """
    owner = await _owner(session)
    document = await _seed_document(
        session,
        storage,
        owner_id=owner,
        payload=_PAYLOAD,
        provenance=_provenance(embedding_model="old"),
    )

    outcome = await rebuild_document(
        document_id=document.id,
        actor_id=None,
        session=session,
        storage=storage,
        queue=queue,
    )

    assert outcome.version == 2
    assert outcome.previous_version == 1
    new_key = _version_key(document, version=2)
    assert await storage.exists(new_key), "the v2 original must exist before the worker purges v1"
    assert await storage.get(new_key) == _PAYLOAD, "the same bytes, not a re-read of something else"
    await session.refresh(document)
    assert document.storage_uri.endswith(new_key), "and storage_uri must name it"
    # v1 is still there: the worker purges it after its own swap commits (R-40(3)).
    assert await storage.exists(_version_key(document, version=1))


async def test_a_rebuild_leaves_the_indexed_version_serving(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """R-36(3) through a real retrieval query, on `test_replace.py`'s reasoning.

    Asserting `current_version == 1` alone would pass against a retrieval layer that had begun
    filtering `Document.status` — and a rebuild sets the status to `QUEUED`, so that is exactly
    the regression this guarantee exists to prevent.
    """
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    assert len(await _search(session, owner)) == 1

    await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    await session.refresh(document)
    assert document.current_version == 1, "the pointer moves in the worker's swap, not here"
    assert document.searchable is True
    assert document.chunk_count == 1
    assert document.page_count == 58
    assert document.status is DocumentStatus.QUEUED
    assert len(await _search(session, owner)) == 1, "it must still answer questions"


async def test_a_rebuild_leaves_the_bytes_metadata_alone(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """Same bytes, so the columns describing them must not be rewritten from a second read."""
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    before = (
        document.checksum_sha256,
        document.size_bytes,
        document.filename,
        document.mime_type,
    )

    await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    await session.refresh(document)
    assert (
        document.checksum_sha256,
        document.size_bytes,
        document.filename,
        document.mime_type,
    ) == before


async def test_a_rebuild_queues_one_job_at_the_next_version(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )

    outcome = await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    jobs = await _jobs_for(session, document.id)
    assert len(jobs) == 1
    assert jobs[0].job_type is JobType.INGEST
    assert jobs[0].document_version == 2
    assert jobs[0].idempotency_key == f"ingest:{document.id}:v2"
    assert [call["job_id"] for call in queue.ingests] == [outcome.job_id]


async def test_the_target_version_clears_a_burned_number(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """`MAX(knowledge_jobs.document_version) + 1`, never `current_version + 1`.

    A replace whose ingestion failed has already burned v2 and its `ingest:{doc}:v2` key is
    unique, so reusing the number is an `IntegrityError` rather than a rebuild.
    """
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    session.add(
        KnowledgeJob(
            document_id=document.id,
            job_type=JobType.INGEST,
            status=JobStatus.FAILED,
            document_version=2,
            idempotency_key=f"ingest:{document.id}:v2",
        )
    )
    await session.flush()

    outcome = await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )
    assert outcome.version == 3


async def test_an_open_ingest_job_is_superseded(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """ "At most one open INGEST job per document" is what the swap guard reasons from."""
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    stale_job = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.QUEUED,
        document_version=1,
        idempotency_key=f"ingest:{document.id}:v1",
    )
    session.add(stale_job)
    await session.flush()

    await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    await session.refresh(stale_job)
    assert stale_job.status is JobStatus.FAILED
    assert stale_job.error_code == SUPERSEDED


# ---- 4. the three refusals ---------------------------------------------------


async def test_a_document_built_by_the_configured_pipeline_is_refused(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """R-84(3), and the reason this is a trigger rather than a button.

    A re-embed that will change nothing is not a cheaper re-embed: it is a full-price one whose
    result is byte-identical. Without this the route is exactly the widened `/retry` the board
    line forbade.
    """
    owner = await _owner(session)
    document = await _seed_document(session, storage, owner_id=owner)

    with pytest.raises(NotStaleError):
        await rebuild_document(
            document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
        )

    assert await _jobs_for(session, document.id) == []
    assert queue.ingests == []
    assert not await storage.exists(_version_key(document, version=2))


@pytest.mark.parametrize(
    "status", [DocumentStatus.FAILED, DocumentStatus.QUEUED, DocumentStatus.DELETE_PENDING]
)
async def test_a_document_that_is_not_active_is_refused(
    session: AsyncSession,
    storage: _CountingStorage,
    queue: _RecordingQueue,
    status: DocumentStatus,
) -> None:
    owner = await _owner(session)
    document = await _seed_document(
        session,
        storage,
        owner_id=owner,
        status=status,
        provenance=_provenance(embedding_model="old"),
    )

    with pytest.raises(NotRebuildableError):
        await rebuild_document(
            document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
        )
    assert await _jobs_for(session, document.id) == []


async def test_a_corrupt_original_is_refused_before_a_version_is_burned(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """R-84(8). Rebuilding anyway would record different bytes under the old checksum, and that
    column is the one FR-KBM-08's dedup trusts — so the next upload of the *real* file would be
    answered as a duplicate of a document that no longer contains it."""
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    await storage.put(_version_key(document, version=1), b"%PDF-1.7\ntampered\n%%EOF\n")

    with pytest.raises(OriginalCorruptError):
        await rebuild_document(
            document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
        )

    assert await _jobs_for(session, document.id) == []
    assert not await storage.exists(_version_key(document, version=2))
    await session.refresh(document)
    assert document.status is DocumentStatus.ACTIVE, "nothing was written"


async def test_a_refusal_costs_no_download(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """Both gates run **before** the object is fetched, and only the absence proves it.

    Mutation testing is why this test exists. `rebuild_document` checks the state and the
    staleness twice — once up front, once under the row lock — so removing either of the first
    pair leaves the refusal intact and every other assertion in this module green: the second
    pair catches it. What changes is that the server has just pulled up to 50 MB out of object
    storage, under the upload semaphore, to answer a request it was always going to refuse.

    R-63(6)'s shape, one subject over: *a check that must happen before a download is asserted
    by the absence of the download.*
    """
    owner = await _owner(session)
    fresh = await _seed_document(session, storage, owner_id=owner)
    inactive = await _seed_document(
        session,
        storage,
        owner_id=owner,
        status=DocumentStatus.FAILED,
        provenance=_provenance(embedding_model="old"),
    )
    # Seeding wrote the originals through this same double; only what the *rebuild* does counts.
    storage.gets = 0
    storage.puts.clear()

    for document, expected in ((fresh, NotStaleError), (inactive, NotRebuildableError)):
        with pytest.raises(expected):
            await rebuild_document(
                document_id=document.id,
                actor_id=None,
                session=session,
                storage=storage,
                queue=queue,
            )

    assert storage.gets == 0, "a refused rebuild must not read the original"
    assert storage.puts == [], "and must certainly not write one"


async def test_a_document_that_stops_being_eligible_mid_download_is_refused(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """The second pair of gates — the ones under the row lock — and the only way to reach them.

    The download is the window: minutes for a large original, and precisely when a concurrent
    delete, replace or second rebuild lands. The first pair cannot see any of that, because it
    ran before.

    Two cases, because they fail for different reasons and neither implies the other. A document
    that stopped being `ACTIVE` is the delete/replace race. A document that stopped being *stale*
    is the one the state gate cannot catch at all: a **completed** concurrent rebuild leaves it
    `ACTIVE` and current, so without the staleness re-read the second rebuild would burn another
    version to produce byte-identical vectors.
    """
    owner = await _owner(session)

    async def _fail_it() -> None:
        document.status = DocumentStatus.FAILED
        await session.flush()

    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    storage.on_get = _fail_it
    storage.gets = 0
    storage.puts.clear()
    with pytest.raises(NotRebuildableError):
        await rebuild_document(
            document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
        )
    assert storage.gets == 1, "this case must be reached *through* the download, not before it"
    assert await _jobs_for(session, document.id) == []

    async def _freshen_it() -> None:
        for chunk in await DocumentChunkRepository(session).list_by_document(second.id):
            chunk.meta = {**chunk.meta, "embedding_model": configured_pipeline().embedding_model}
        await session.flush()

    second = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    storage.on_get = _freshen_it
    storage.puts.clear()
    with pytest.raises(NotStaleError):
        await rebuild_document(
            document_id=second.id, actor_id=None, session=session, storage=storage, queue=queue
        )
    assert await _jobs_for(session, second.id) == []
    assert storage.puts == [], "no version may be burned by a refusal under the lock"


def test_the_enqueue_is_the_last_thing_and_happens_after_the_commit() -> None:
    """A source guard, because no unit test on one connection can observe this.

    Enqueueing inside the transaction is the classic dual-write bug: the worker can pick up a
    `document_id` no other connection can see yet, and on a 50 MB copy that window is real. The
    harness shares one savepoint-joined connection with the app, so visibility is not observable
    from here — `test_graph.py`'s idiom applies, and the ordering is pinned by reading the source
    the way T-506 pins `setMentionOpen` and T-513 pins `turnInFlight`.
    """
    import inspect

    from app.services import documents as documents_module

    source = inspect.getsource(documents_module.rebuild_document)
    body = source.split('"""', 2)[-1]
    commit = body.rindex("await session.commit()")
    enqueue = body.index("_enqueue_quietly")
    assert enqueue > commit, "the enqueue must follow the commit that makes the job row durable"
    # And outside the `try`, or a `JobQueueError` would be swallowed by the compensating
    # rollback that deletes the object the job is about to be told to read.
    assert body[commit:enqueue].count("except Exception:") == 1


async def test_an_unknown_document_is_not_found(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    with pytest.raises(DocumentNotFoundError):
        await rebuild_document(
            document_id=uuid.uuid4(),
            actor_id=None,
            session=session,
            storage=storage,
            queue=queue,
        )


async def test_a_missing_original_leaves_nothing_written(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    owner = await _owner(session)
    document = await _seed_document(
        session,
        storage,
        owner_id=owner,
        provenance=_provenance(embedding_model="old"),
        store_object=False,
    )

    with pytest.raises(ObjectStorageError):
        await rebuild_document(
            document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
        )

    assert await _jobs_for(session, document.id) == []
    await session.refresh(document)
    assert document.status is DocumentStatus.ACTIVE


# ---- 5. the audit row and the enqueue --------------------------------------


async def test_the_audit_row_is_a_replace_and_names_the_rebuild(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """`record_document_event`'s map defaults to `DOCUMENT_DELETE`, so a missing entry files a
    rebuild in a security artefact as a deletion — the exact miss T-209 fixed for `replace`."""
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )

    await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    stmt = select(AuditLog).where(AuditLog.target_id == str(document.id))
    rows = list((await session.scalars(stmt)).all())
    assert len(rows) == 1
    assert rows[0].event_type is AuditEventType.DOCUMENT_REPLACE
    assert rows[0].details == {"action": "rebuild"}
    assert rows[0].actor_id is None, "the CLI has no principal, and the column allows that"


async def test_the_audit_row_names_the_administrator_on_the_route(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: _CountingStorage,
    make_token: Callable[..., str],
) -> None:
    admin, headers = await _caller(session, make_token, admin=True)
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )

    response = await client.post(f"/api/v1/admin/documents/{document.id}/reembed", headers=headers)
    assert response.status_code == 202, response.text

    stmt = select(AuditLog).where(AuditLog.target_id == str(document.id))
    row = (await session.scalars(stmt)).one()
    # `upsert_from_claims` stores `users.id = sub`, so the administrator's subject *is* the
    # actor id the foreign key wants.
    assert row.actor_id == admin


async def test_a_failed_enqueue_leaves_the_job_recoverable(
    session: AsyncSession, storage: _CountingStorage, queue: _RecordingQueue
) -> None:
    """The upload path's rule: the row is committed and `QUEUED`, so the sweeper can find it."""
    owner = await _owner(session)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    queue.fail = True

    outcome = await rebuild_document(
        document_id=document.id, actor_id=None, session=session, storage=storage, queue=queue
    )

    job = (await _jobs_for(session, document.id))[0]
    assert job.id == outcome.job_id
    assert job.status is JobStatus.QUEUED
    assert job.error_code == ENQUEUE_FAILED


# ---- 6. the admin routes ----------------------------------------------------


async def test_the_stale_route_lists_and_prices_the_work(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: _CountingStorage,
    make_token: Callable[..., str],
) -> None:
    _admin, headers = await _caller(session, make_token, admin=True)
    owner = await _owner(session)
    document = await _seed_document(
        session,
        storage,
        owner_id=owner,
        provenance=_provenance(embedding_model="old"),
        token_count=250,
    )

    response = await client.get(
        "/api/v1/admin/documents/stale", params={"owner_id": str(owner)}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pipeline"]["embedding_model"] == configured_pipeline().embedding_model
    assert body["totals"] == {
        "documents": 1,
        "chunks": 1,
        "token_count": 250,
        "in_flight": 0,
    }
    assert [row["document_id"] for row in body["documents"]] == [str(document.id)]
    assert body["documents"][0]["drifted_inputs"] == ["embedding_model"]
    assert "storage_uri" not in body["documents"][0], "metadata only (R-40(5))"


async def test_the_stale_route_is_administrator_only(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _owner, headers = await _caller(session, make_token)
    response = await client.get("/api/v1/admin/documents/stale", headers=headers)
    assert response.status_code == 403


async def test_the_reembed_route_answers_202_with_both_versions(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: _CountingStorage,
    make_token: Callable[..., str],
    queue: _RecordingQueue,
) -> None:
    _admin, headers = await _caller(session, make_token, admin=True)
    document = await _seed_document(
        session,
        storage,
        owner_id=await _owner(session),
        provenance=_provenance(embedding_model="old"),
    )

    response = await client.post(f"/api/v1/admin/documents/{document.id}/reembed", headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["previous_version"] == 1
    assert body["status"] == DocumentStatus.QUEUED.value
    assert len(queue.ingests) == 1


async def test_the_reembed_route_is_administrator_only(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: _CountingStorage,
    make_token: Callable[..., str],
) -> None:
    """A user may not re-embed even their own document: it is an operator cost decision."""
    owner, headers = await _caller(session, make_token)
    document = await _seed_document(
        session, storage, owner_id=owner, provenance=_provenance(embedding_model="old")
    )
    response = await client.post(f"/api/v1/admin/documents/{document.id}/reembed", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DocumentStatus.ACTIVE, "NOT_STALE"),
        (DocumentStatus.FAILED, "NOT_REBUILDABLE"),
    ],
)
async def test_the_reembed_route_names_which_conflict(
    client: httpx.AsyncClient,
    session: AsyncSession,
    storage: _CountingStorage,
    make_token: Callable[..., str],
    status: DocumentStatus,
    code: str,
) -> None:
    """R-71(1): a client cannot tell two `409`s apart without a code, and these three resolve
    differently — one is nothing to do, one is a state to wait out, one needs a human."""
    _admin, headers = await _caller(session, make_token, admin=True)
    provenance = None if status is DocumentStatus.ACTIVE else _provenance(embedding_model="old")
    document = await _seed_document(
        session, storage, owner_id=await _owner(session), status=status, provenance=provenance
    )

    response = await client.post(f"/api/v1/admin/documents/{document.id}/reembed", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == code


async def test_the_reembed_route_404s_for_an_unknown_document(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _admin, headers = await _caller(session, make_token, admin=True)
    response = await client.post(f"/api/v1/admin/documents/{uuid.uuid4()}/reembed", headers=headers)
    assert response.status_code == 404


# ---- 7. the CLI -------------------------------------------------------------


async def test_the_plan_renders_the_pipeline_the_cost_and_ascii_only(
    session: AsyncSession, storage: _CountingStorage, capsys
) -> None:  # noqa: ANN001
    """R-80(7)'s lesson one tool over: prose keeps the em dashes, **stdout is ASCII**."""
    owner = await _owner(session)
    await _seed_document(
        session,
        storage,
        owner_id=owner,
        provenance=_provenance(embedding_model="old"),
        token_count=1234,
    )
    plan = await plan_reembed(session, owner_id=owner)

    text = "\n".join(
        [
            *reembed_cli._pipeline_lines(plan),
            *reembed_cli._totals_lines(plan),
            *reembed_cli._table(plan),
        ]
    )
    text.encode("ascii")  # must not raise
    text.encode("cp1252")
    assert configured_pipeline().embedding_model in text
    assert "1,234" in text, "the cost has to be in the table, not only in the object"
    assert "embedding_model" in text


def test_an_empty_plan_says_so(capsys) -> None:  # noqa: ANN001
    """A report whose answer is "nothing" must say it in words, not by printing no rows."""
    from app.db.repositories.chunks import StalePipelineTotals
    from app.services.reembed import ReembedPlan

    plan = ReembedPlan(
        pipeline=configured_pipeline(),
        totals=StalePipelineTotals(documents=0, chunks=0, token_count=0, in_flight=0),
        documents=(),
    )
    assert reembed_cli._table(plan) == []
    assert "0 document(s)" in "\n".join(reembed_cli._totals_lines(plan))


def test_run_requires_an_explicit_limit() -> None:
    """There is deliberately no way to type "all of them" (R-84(5)/(9))."""
    with pytest.raises(SystemExit) as exc:
        reembed_cli.main(["run"])
    assert exc.value.code == 2


def test_the_table_says_when_it_is_showing_a_page() -> None:
    """No silent caps: a page that looks like the whole set is how "covered everything" gets
    believed about a bounded read."""
    from app.db.repositories.chunks import StalePipelineDocument, StalePipelineTotals
    from app.services.reembed import ReembedPlan

    row = StalePipelineDocument(
        document_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        filename="handbook.pdf",
        document_version=1,
        chunk_count=1,
        token_count=10,
        embedding_model_drift=True,
        chunking_version_drift=False,
        preprocessing_version_drift=False,
    )
    plan = ReembedPlan(
        pipeline=configured_pipeline(),
        totals=StalePipelineTotals(documents=9, chunks=9, token_count=90, in_flight=0),
        documents=(row,),
    )
    assert "showing 1 of 9" in "\n".join(reembed_cli._table(plan))
