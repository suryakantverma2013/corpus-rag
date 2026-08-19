"""Harness for the §11 acceptance scenarios (T-601).

The one piece of infrastructure this package needed and the suite did not have: a job queue
double that **records on enqueue and runs the real worker task on demand**.

Running the worker inline at enqueue time would be simpler and cannot express half the
table. §11 rows 3 and 4 are interleavings — a delete arriving *while* an ingest is still
pending — so the test has to own the moment the worker runs. Hence `DrainingQueue.drain()`.

**The wiring detail that makes it work.** The worker calls real `commit()`, so it needs an
`async_sessionmaker` rather than the shared `session` fixture; but it must bind to the *same*
`db_connection` the ASGI app is overridden onto, or it will not see the rows the HTTP upload
just wrote and every scenario will fail as "document not found". This is exactly the pattern
`tests/conftest.py::app` already uses for the R-41 event stream's per-tick session.

`QUEUE_BACKEND` stays `none` — `tests/test_harness.py::test_the_suite_never_builds_a_real_job_queue`
asserts the suite never constructs a real broker, and `dependency_overrides` is the sanctioned
seam.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from arq.worker import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

import workers.delete
import workers.evaluate
import workers.ingest
from app.config import ScannerSettings, Settings, WorkerSettings, get_settings
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.users import UserRepository
from app.ingestion.scanner import ScanResult
from app.services.embeddings import FakeEmbeddingClient
from app.services.jobs import JobQueueError, get_job_queue
from app.services.object_storage import LocalFilesystemStorage, get_object_storage

# --- document builders ----------------------------------------------------------------

#: Distinctive enough that a retrieval hit is attributable. Scenario corpora stay tiny on
#: purpose: these are the slowest tests in the suite and a second page buys nothing.
CORPUS_TEXT = (
    "Corpus ingests documents and answers questions strictly from their contents. "
    "The perihelion precession of Mercury is the rare lexical anchor in this passage."
)


def pdf(text: str = CORPUS_TEXT, *, pages: tuple[str, ...] = ()) -> bytes:
    """A real, parseable PDF — not `test_upload.py`'s stub header.

    The upload route never parses, so its `_pdf` can be four lines of bytes. Every scenario
    here runs the actual worker, so the bytes have to survive PyMuPDF.
    """
    document = pymupdf.open()
    for body in pages or (text,):
        page = document.new_page()
        page.insert_text((72, 720), body)
    raw = document.tobytes()
    document.close()
    return raw


def upload_files(
    payload: bytes, filename: str = "handbook.pdf", mime: str = "application/pdf"
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, payload, mime)}


# --- the draining queue ---------------------------------------------------------------


@dataclass(frozen=True)
class Enqueued:
    """One dispatch the application asked for."""

    kind: str  # "ingest" | "delete" | "evaluate"
    idempotency_key: str
    job_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None


@dataclass
class Ran:
    """The outcome of running one job, with the retry signal captured rather than raised."""

    job: Enqueued
    retry: Retry | None = None
    error: BaseException | None = None

    @property
    def retried(self) -> bool:
        """arq was asked to redeliver this job — §11 row 6's whole claim."""
        return self.retry is not None


class DrainingQueue:
    """Records enqueues; runs the real worker tasks on demand, FIFO.

    `enqueue_evaluate` is recorded but **not** run by `drain()`: an answered turn enqueues a
    DeepEval job, and a scenario about ingestion should not silently start calling a judge.
    `drain_evaluations()` runs them for the tests that want them.
    """

    def __init__(self, ctx_factory: Callable[..., dict[str, Any]]) -> None:
        self._ctx_factory = ctx_factory
        self.pending: list[Enqueued] = []
        self.dispatched: list[Enqueued] = []
        self.ran: list[Ran] = []
        self.fail = False

    # -- the JobQueue protocol (app/services/jobs.py) --

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self._record(Enqueued("ingest", idempotency_key, job_id=job_id, document_id=document_id))

    async def enqueue_delete(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self._record(Enqueued("delete", idempotency_key, job_id=job_id, document_id=document_id))

    async def enqueue_evaluate(self, *, message_id: uuid.UUID, idempotency_key: str) -> None:
        self._record(Enqueued("evaluate", idempotency_key, message_id=message_id))

    async def aclose(self) -> None:
        return None

    # -- draining --

    def _record(self, item: Enqueued) -> None:
        if self.fail:
            raise JobQueueError("broker down")
        self.pending.append(item)
        self.dispatched.append(item)

    def _take(self, kinds: tuple[str, ...]) -> list[Enqueued]:
        taken = [item for item in self.pending if item.kind in kinds]
        self.pending = [item for item in self.pending if item.kind not in kinds]
        return taken

    async def drain(self, *, job_try: int = 1, **ctx_overrides: Any) -> list[Ran]:
        """Run every pending ingest/delete, in the order the application asked for them."""
        return [
            await self._run(item, job_try=job_try, **ctx_overrides)
            for item in self._take(("ingest", "delete"))
        ]

    async def drain_evaluations(self, *, job_try: int = 1, **ctx_overrides: Any) -> list[Ran]:
        return [
            await self._run(item, job_try=job_try, **ctx_overrides)
            for item in self._take(("evaluate",))
        ]

    async def redeliver(self, job: Enqueued, *, job_try: int = 2, **ctx_overrides: Any) -> Ran:
        """Re-run a job arq would have redelivered, under a later attempt number.

        This is the only way to express FR-ING-04 honestly: the broker hands back the *same*
        `job_id` and `idempotency_key`, and the task has to cope. Re-uploading would mint a
        new job and prove nothing.
        """
        return await self._run(job, job_try=job_try, **ctx_overrides)

    async def _run(self, item: Enqueued, *, job_try: int, **ctx_overrides: Any) -> Ran:
        ctx = self._ctx_factory(job_try=job_try, **ctx_overrides)
        outcome = Ran(job=item)
        try:
            if item.kind == "ingest":
                assert item.document_id is not None and item.job_id is not None
                await workers.ingest.ingest_document(ctx, str(item.document_id), str(item.job_id))
            elif item.kind == "delete":
                assert item.document_id is not None and item.job_id is not None
                await workers.delete.delete_document(ctx, str(item.document_id), str(item.job_id))
            else:
                assert item.message_id is not None
                await workers.evaluate.evaluate_message(ctx, str(item.message_id))
        except Retry as retry:
            # arq's redelivery signal is a control-flow exception, not a failure. Capturing
            # it is what lets a test assert "this job asked to be retried" rather than
            # "this job blew up", which is the distinction §11 row 6 turns on.
            outcome.retry = retry
        self.ran.append(outcome)
        return outcome


class StubScanner:
    """R-32's screen, stubbed. The real screens have their own suite (`test_scanner.py`)."""

    def __init__(self, result: ScanResult | None = None, error: Exception | None = None) -> None:
        self.result = result or ScanResult.clean()
        self.error = error
        self.calls: list[str] = []

    async def scan(self, payload: bytes, *, filename: str) -> ScanResult:
        self.calls.append(filename)
        if self.error is not None:
            raise self.error
        return self.result


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "objects")


@pytest.fixture
def embedder() -> FakeEmbeddingClient:
    """One client for the whole scenario, so `embedded_inputs` is a running total.

    Rows 2 and 7 are claims about *how many* chunks were embedded, so the counter has to
    survive across drains — a fresh client per job would reset the evidence.

    **Constructed with the configured model id, exactly as `build_embedding_client` does.** A
    bare `FakeEmbeddingClient()` answers `"fake-embedding"`, and `deps.embedder.model` is what
    reaches `compute_embedding_fingerprint` — so the harness would stamp every chunk with a
    model no deployment configures, and T-608's drift report would call a freshly ingested
    document stale (found exactly that way, by row 7).
    """
    return FakeEmbeddingClient(get_settings().openai.embedding_model)


@pytest.fixture
def scanner() -> StubScanner:
    return StubScanner()


@pytest.fixture
def worker_sessions(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions for the worker, on the **same** connection the ASGI app is bound to.

    The worker commits for real; under this binding those commits become savepoint
    releases inside the fixture's outer transaction, so the suite still leaves nothing
    behind — and, crucially, the worker can see rows the HTTP request wrote.
    """
    return async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
def worker_ctx(
    worker_sessions: async_sessionmaker[AsyncSession],
    storage: LocalFilesystemStorage,
    embedder: FakeEmbeddingClient,
    scanner: StubScanner,
) -> Callable[..., dict[str, Any]]:
    """Build an arq context with production's key set (`workers/main.py::on_startup`)."""

    def factory(
        *,
        job_try: int = 1,
        max_tries: int = 5,
        embedder_override: Any = None,
        settings_override: Settings | None = None,
    ) -> dict[str, Any]:
        return {
            "settings": settings_override
            or Settings(
                worker=WorkerSettings(max_tries=max_tries, retry_base_seconds=0.01),
                scanner=ScannerSettings(backend="structural"),
            ),
            "sessionmaker": worker_sessions,
            "storage": storage,
            "embedder": embedder_override or embedder,
            "scanner": scanner,
            "job_try": job_try,
        }

    return factory


@pytest.fixture
def queue(worker_ctx: Callable[..., dict[str, Any]]) -> DrainingQueue:
    return DrainingQueue(worker_ctx)


@pytest.fixture
def app(app, storage: LocalFilesystemStorage, queue: DrainingQueue):  # noqa: ANN001, ANN201
    """The shared app, plus the storage and queue this package drives."""
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


@pytest.fixture(autouse=True)
def _serial_retrieval() -> Iterator[None]:
    """One connection cannot hold two concurrent savepoints.

    T-311's prefetch runs the original query's retrieval arm alongside the router call, and
    under the transactional fixture that is a second savepoint on the same connection. The
    same fixture exists in `test_chat_api.py` and `test_chat_live.py`; it is autouse here
    because any scenario may end up asking a question.
    """
    retrieval = get_settings().retrieval
    previous = retrieval.prefetch_query_arm
    retrieval.prefetch_query_arm = False
    try:
        yield
    finally:
        retrieval.prefetch_query_arm = previous


# --- assertion helpers ----------------------------------------------------------------
#
# The suite runs against the shared local `corpus` database and nothing truncates it, so
# every query here is scoped to the caller the test minted (the T-109 rule). Asserting over
# a whole table would couple these tests to whatever else happens to be in the database.


@dataclass(frozen=True)
class Caller:
    """A seeded user and the headers that authenticate them."""

    id: uuid.UUID
    email: str
    headers: dict[str, str] = field(default_factory=dict)


async def make_caller(
    session: AsyncSession,
    make_token: Callable[..., str],
    *,
    email: str | None = None,
    roles: tuple[str, ...] = ("user",),
) -> Caller:
    sub = uuid.uuid4()
    address = email or f"{sub.hex[:12]}@corpus.test"
    user = await UserRepository(session).upsert_from_claims(
        sub=sub, email=address, display_name="Scenario User"
    )
    token = make_token(sub=sub, email=address, roles=roles)
    return Caller(id=user.id, email=address, headers={"Authorization": f"Bearer {token}"})


async def documents_of(session: AsyncSession, owner_id: uuid.UUID) -> list[Document]:
    stmt = select(Document).where(Document.owner_id == owner_id).order_by(Document.created_at)
    return list((await session.scalars(stmt)).all())


async def jobs_of(session: AsyncSession, document_id: uuid.UUID) -> list[KnowledgeJob]:
    stmt = (
        select(KnowledgeJob)
        .where(KnowledgeJob.document_id == document_id)
        .order_by(KnowledgeJob.created_at)
    )
    return list((await session.scalars(stmt)).all())


async def chunks_of(session: AsyncSession, document_id: uuid.UUID) -> list[DocumentChunk]:
    stmt = (
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.document_version, DocumentChunk.chunk_index)
    )
    return list((await session.scalars(stmt)).all())


async def reload(session: AsyncSession, model: type, pk: uuid.UUID) -> Any:
    """Re-read one row, ignoring the identity map — the worker committed on another session.

    `populate_existing=True` rather than `session.expire_all()`: expiring the whole session
    invalidates every instance the test is already holding, and the next attribute read on
    one of those raises `MissingGreenlet` from inside an assertion, which reads as a defect
    in the code under test rather than in the helper.
    """
    return await session.get(model, pk, populate_existing=True)


async def sse_frames(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into `(event, payload)` pairs, as `test_chat_api.py` does."""
    import json

    frames: list[tuple[str, dict[str, Any]]] = []
    event: str | None = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            # The `data:` line carries the whole envelope (R-57); the `event:` line must
            # agree with it or the client's discriminator and its dispatcher disagree.
            assert event == payload.get("event"), (event, payload)
            frames.append((payload["event"], payload.get("data") or {}))
            event = None
    return frames
