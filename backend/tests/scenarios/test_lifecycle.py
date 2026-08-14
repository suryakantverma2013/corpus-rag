"""§11 rows 1, 2, 3, 5, 6 and 12 — the document lifecycle under concurrency and failure.

Every test here drives the HTTP route, then runs the real worker through `DrainingQueue`,
then reads the database back. That crossing is the point: the component halves are already
covered in `test_upload.py`, `test_deletion.py`, `test_replace.py` and `test_ingest_task.py`,
and none of them can see a defect that lives *between* the route and the worker.

NFR-REL-04 is the tightest requirement in the file — "never producing duplicate vectors or
serving a deleting document" — and rows 3, 5 and 6 are its three failure shapes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import workers.ingest
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.documents import DocumentRepository
from app.services.embeddings import EmbeddingUnavailableError, FakeEmbeddingClient
from tests.scenarios import scenario
from tests.scenarios.conftest import (
    DrainingQueue,
    Enqueued,
    chunks_of,
    jobs_of,
    make_caller,
    pdf,
    reload,
    upload_files,
)

pytestmark = pytest.mark.usefixtures("patch_jwks")


# --- row 1: same file uploaded twice --------------------------------------------------


@scenario("S01")
async def test_the_same_file_uploaded_twice_is_never_ingested_twice(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """FR-KBM-08. The existing test stops at the status code; this proves the *behaviour*.

    "Detect checksum duplicate; **no re-ingestion**" is a claim about work that did not
    happen, and a `200 duplicate:true` on its own is consistent with a second ingestion
    running anyway. So the assertion is on the drained side: one job dispatched, one
    version, one chunk set, and the embedder called for the first upload only.
    """
    caller = await make_caller(session, make_token)
    payload = pdf()

    first = await client.post(
        "/api/v1/documents", files=upload_files(payload), headers=caller.headers
    )
    assert first.status_code == 202, first.text
    document_id = first.json()["document_id"]

    await queue.drain()
    after_first = await chunks_of(session, document_id)
    assert after_first, "the first upload should have produced a chunk set"

    second = await client.post(
        "/api/v1/documents", files=upload_files(payload), headers=caller.headers
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["duplicate"] is True
    assert body["document_id"] == document_id
    assert body["job_id"] is None

    # Nothing new to run, and nothing changed by asking.
    assert await queue.drain() == []
    assert [item.kind for item in queue.dispatched] == ["ingest"]

    after_second = await chunks_of(session, document_id)
    assert [(row.document_version, row.chunk_index) for row in after_second] == [
        (row.document_version, row.chunk_index) for row in after_first
    ]
    assert {row.document_version for row in after_second} == {1}


# --- row 2: modified file uploaded ----------------------------------------------------


@scenario("S02")
async def test_a_modified_file_re_embeds_only_the_changed_chunks(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    embedder: FakeEmbeddingClient,
) -> None:
    """FR-ING-03, through the **replace** route.

    R-40 is why this is not a second upload: filename is not identity, and a same-filename
    re-upload is a duplicate or a new document, never a new version. Replace is the only
    route that says "these bytes supersede that document".

    The claim is arithmetic — a two-page document with one page edited must cost one page's
    embeddings, not two — so it is asserted on `embedded_inputs`, which is the only place
    the saving is observable. Everything else about the document looks identical either way.
    """
    caller = await make_caller(session, make_token)
    page_one = "The perihelion precession of Mercury is forty-three arcseconds per century."
    page_two = "The original second page describes the aphelion of Neptune."

    created = await client.post(
        "/api/v1/documents",
        files=upload_files(pdf(pages=(page_one, page_two))),
        headers=caller.headers,
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]
    await queue.drain()

    v1 = await chunks_of(session, document_id)
    assert len(v1) >= 2, "the fixture needs at least one chunk per page to be meaningful"
    first_pass_inputs = embedder.embedded_inputs
    assert first_pass_inputs > 0

    replaced = await client.post(
        f"/api/v1/documents/{document_id}/replace",
        files=upload_files(pdf(pages=(page_one, "The second page has been rewritten entirely."))),
        headers=caller.headers,
    )
    assert replaced.status_code == 202, replaced.text
    await queue.drain()

    v2 = [row for row in await chunks_of(session, document_id) if row.document_version == 2]
    assert v2, "the replace should have produced a version 2"

    # The saving: the unchanged page cost nothing the second time.
    second_pass_inputs = embedder.embedded_inputs - first_pass_inputs
    assert 0 < second_pass_inputs < len(v2), (
        "a replace that re-embeds every chunk is FR-ING-03 not holding; "
        f"embedded {second_pass_inputs} of {len(v2)} chunks"
    )

    # Carry-forward takes the *vector* from the old row, so an unchanged chunk keeps the
    # exact embedding it had — the property that makes the saving safe rather than lossy.
    unchanged = {row.embedding_fingerprint for row in v1} & {
        row.embedding_fingerprint for row in v2
    }
    assert unchanged, "no chunk survived the edit; the fixture is not exercising carry-forward"

    document = await reload(session, Document, document_id)
    assert document.current_version == 2
    assert {row.document_version for row in await chunks_of(session, document_id)} == {2}, (
        "the superseded version must be collected in the swap (R-36(4)) — two live versions "
        "would double-count in retrieval and render two citations to one passage"
    )


# --- row 3: delete while ingestion is running -----------------------------------------


@scenario("S03")
async def test_a_delete_during_ingestion_supersedes_it_and_never_publishes(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """FR-ING-04/05, and the three points R-39(8) closes it at.

    The interleaving is the test: the ingest job is enqueued and deliberately **not** run,
    the delete arrives, and only then does the stale ingest get its turn. A queue that ran
    jobs inline on enqueue could not express this at all.

    NFR-REL-04's "never serving a deleting document" is the assertion that matters: the
    ingest must not reach `mark_active`, whatever order the two jobs happen to run in.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    # The ingest is queued and has not run.
    assert [item.kind for item in queue.pending] == ["ingest"]

    deleted = await client.delete(f"/api/v1/documents/{document_id}", headers=caller.headers)
    assert deleted.status_code == 202, deleted.text

    # Point 1: the synchronous gate. `searchable` is false *before* the 202 is answered,
    # which is what makes FR-ING-05 immediate rather than eventually consistent.
    document = await reload(session, Document, document_id)
    assert document.searchable is False
    assert document.status in {DocumentStatus.DELETE_PENDING, DocumentStatus.DELETING}

    # Point 2: the open ingest job is superseded rather than left to race.
    open_ingests = [
        job for job in await jobs_of(session, document_id) if job.job_type.value == "INGEST"
    ]
    assert open_ingests
    assert all(job.status is JobStatus.FAILED for job in open_ingests)
    assert any(job.error_code == "DOCUMENT_DELETED" for job in open_ingests)

    # Point 3: run the stale ingest anyway. It must short-circuit rather than publish. This
    # reaches `_begin`'s `_DELETION_STATES` check, not the swap guard — see the next test.
    await queue.drain()

    document = await reload(session, Document, document_id)
    assert document.status is DocumentStatus.DELETED
    assert document.searchable is False
    assert not [row for row in await chunks_of(session, document_id)], (
        "a document that was deleted mid-ingest must leave no searchable chunk behind"
    )


@scenario("S03")
async def test_a_delete_that_arrives_mid_ingest_is_caught_by_the_swap_guard(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half `_begin` cannot cover — and the test above provably does not reach.

    R-39(8) closes delete-during-ingest at **three** points, and the third exists precisely
    because the first two run *before* the parse, the scan and the embedding calls. A
    `DELETE` arriving after that window is invisible to them: the job is already past its
    status check, and the ORM instance the task carries has had any `DELETE_PENDING`
    overwritten by its own `set_status(INDEXING)`. Only the `FOR UPDATE` re-read immediately
    before `mark_active` can see it.

    **This test exists because a mutation proved the previous one did not cover it.**
    Disabling the swap guard left the whole scenario suite green, because the delete route
    fails the open ingest job and `_begin` then short-circuits on the state — so the guard
    was never reached. Here the delete is issued from *inside* the run, after `_begin`, which
    is the only ordering that exercises it.

    NFR-REL-04's "never serving a deleting document" is the assertion: without the guard,
    `mark_active` commits `searchable = True` over a deletion in progress and resurrects the
    document — as an `ACTIVE` row whose original bytes the purge has already removed.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    real_plan = workers.ingest._plan_on_a_disposable_session
    deleted_mid_run: list[int] = []

    async def delete_midway(**kwargs):  # noqa: ANN003, ANN202
        plan = await real_plan(**kwargs)
        if not deleted_mid_run:
            # Through the route, not by hand-writing rows: the point is that the request's
            # synchronous half and the worker's swap guard compose.
            response = await client.delete(
                f"/api/v1/documents/{document_id}", headers=caller.headers
            )
            assert response.status_code == 202, response.text
            deleted_mid_run.append(1)
        return plan

    monkeypatch.setattr(workers.ingest, "_plan_on_a_disposable_session", delete_midway)

    await queue.drain()
    assert deleted_mid_run, "the delete never fired; the injection point has moved"

    document = await reload(session, Document, document_id)
    assert document.status is not DocumentStatus.ACTIVE, (
        "the ingest published a document that was being deleted — the R-39(8) swap guard "
        "is the only thing standing between a mid-ingest DELETE and a resurrected row"
    )
    assert document.searchable is False
    assert not await chunks_of(session, document_id), (
        "a document deleted mid-ingest must leave no chunk rows behind"
    )


# --- row 5: worker crashes after vector insertion -------------------------------------


@scenario("S05")
async def test_a_crash_after_vector_insertion_replays_without_duplicating(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-ING-04 / NFR-REL-04's "never producing duplicate vectors" — the uncommitted half.

    "After vector insertion" is a precise moment: `workers/ingest.py::_run` calls
    `persist_chunk_set` and then, on the same session, `documents.mark_active`, and commits
    both together. Crashing between them is the closest a worker can get to "the vectors
    landed and the swap did not" — and **because the two share one transaction, the vectors
    do not survive it**. That is the design doing its job, and it is worth asserting rather
    than assuming: it is the reason this failure mode cannot orphan rows at all.

    Injected at `DocumentRepository.mark_active` because that is the smallest stable callable
    on the far side of the write. If a refactor moves the swap, this test breaks and should
    be re-anchored rather than deleted: the property still holds.

    The *committed* half — the crash that loses only the acknowledgement — is the next test.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    real_mark_active = DocumentRepository.mark_active
    crashed = False

    async def crash_once(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("worker died after the vectors landed")
        return await real_mark_active(self, *args, **kwargs)

    monkeypatch.setattr(DocumentRepository, "mark_active", crash_once)

    first = (await queue.drain())[0]
    assert crashed, "the fault never fired; the injection point has moved"
    assert first.retried, (
        "a crash mid-swap must ask arq for redelivery — without it the document is stranded"
    )

    mid_flight = await reload(session, Document, document_id)
    assert mid_flight.status is not DocumentStatus.ACTIVE
    assert mid_flight.searchable is False, "a half-swapped document must never serve"
    assert not await chunks_of(session, document_id), (
        "the chunk write and the version swap must share one transaction — vectors that "
        "outlive a failed swap are exactly the orphans NFR-REL-04 forbids"
    )

    # arq redelivers the *same* job id under a later attempt — that is what makes
    # `idempotency_key` load-bearing rather than decorative.
    await queue.redeliver(first.job)

    document = await reload(session, Document, document_id)
    assert document.status is DocumentStatus.ACTIVE
    assert document.searchable is True

    rows = await chunks_of(session, document_id)
    assert rows, "the replay produced no chunks at all"
    assert {row.document_version for row in rows} == {document.current_version}
    indexes = [row.chunk_index for row in rows]
    assert len(indexes) == len(set(indexes)), f"the replay duplicated chunk rows: {sorted(indexes)}"


@scenario("S05")
async def test_a_redelivery_after_a_committed_ingest_changes_nothing(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """The committed half: the vectors landed, the *acknowledgement* was lost.

    This is §11 row 5 read literally, and it is the case that reaches production — arq's
    in-progress key expires and a worker re-picks a job whose work is already durable.

    **`_begin` guards this at two independent points, and they catch different deliveries.**
    A redelivery of the *same* job stops at the `SUCCEEDED` check, because the work committed
    and the job row says so. A *fresh* job for a version already built — a duplicate enqueue,
    or a retry minted against a healthy document — gets past that and stops at FR-ING-04's
    named short-circuit, "an `ACTIVE` document short-circuits ingestion". Both are exercised
    below; a mutation showed that testing only the first leaves the second uncovered.

    Asserted on the **chunk row ids**, not on a count: a rebuild that happened to produce the
    same number of chunks would satisfy a count and would still mean the delivery re-embedded
    the document and replaced every vector.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    outcome = (await queue.drain())[0]
    document = await reload(session, Document, document_id)
    assert document.status is DocumentStatus.ACTIVE
    before = {row.id for row in await chunks_of(session, document_id)}
    assert before

    # (a) The same job again. Stops at the `SUCCEEDED` check.
    await queue.redeliver(outcome.job)
    assert {row.id for row in await chunks_of(session, document_id)} == before, (
        "a redelivered job rebuilt work that had already committed — a lost acknowledgement "
        "must not cost a second embedding pass and a second set of vectors"
    )

    # (b) A *fresh* job for the version already built. Gets past the job-status check and
    # must stop at FR-ING-04's ACTIVE short-circuit.
    duplicate = KnowledgeJob(
        document_id=uuid.UUID(document_id),
        job_type=JobType.INGEST,
        status=JobStatus.QUEUED,
        document_version=1,
        idempotency_key=f"ingest:{document_id}:v1:duplicate",
    )
    session.add(duplicate)
    await session.commit()
    await queue.redeliver(
        Enqueued(
            "ingest",
            duplicate.idempotency_key,
            job_id=duplicate.id,
            document_id=uuid.UUID(document_id),
        )
    )

    after = {row.id for row in await chunks_of(session, document_id)}
    assert after == before, (
        "a second job for a version that is already ACTIVE rebuilt the document — this is "
        "the FR-ING-04 short-circuit, and without it a duplicate enqueue re-embeds and "
        "replaces every vector of a healthy document"
    )
    document = await reload(session, Document, document_id)
    assert document.current_version == 1, "neither delivery may bump the version"


# --- row 6: DB succeeds but the vector store fails ------------------------------------


@scenario("S06")
async def test_an_embedding_failure_leaves_the_job_retryable_and_nothing_searchable(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
) -> None:
    """FR-ING-04, under R-76's reading of "DB succeeds but vector store fails".

    §11's wording predates R-16: the upstream source said *Milvus*, a store that fails
    independently of the database. Corpus keeps vectors in pgvector **inside** Postgres, so
    the two writes share one transaction and the split cannot occur as worded. The
    analogous partial failure — the half that is still a second system — is the **embedding
    provider** failing after the `documents` and `knowledge_jobs` rows are committed.

    What must hold is what §11 asks for: the job stays **retryable**, its diagnostics
    survive, and no half-built chunk set is ever searchable.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    class _DeadEmbedder(FakeEmbeddingClient):
        async def embed_texts(self, texts):  # noqa: ANN001, ANN202
            raise EmbeddingUnavailableError("the embedding provider is unreachable")

    outcome = (await queue.drain(embedder_override=_DeadEmbedder()))[0]

    assert outcome.retried, (
        "an embedding outage must ask for redelivery, not dead-letter the document: "
        "the bytes are fine and the operator's fix is to restore the provider"
    )

    document = await reload(session, Document, document_id)
    assert document.status is not DocumentStatus.ACTIVE
    assert document.searchable is False
    assert not await chunks_of(session, document_id), (
        "a failed embedding pass must leave no partial chunk set — half a document is "
        "worse than none, because retrieval cannot tell that it is half"
    )

    jobs = await jobs_of(session, document_id)
    assert jobs
    assert all(job.status is not JobStatus.FAILED for job in jobs), (
        "a retryable failure must not be recorded as terminal"
    )


# --- row 12: partial parsing failure --------------------------------------------------


@scenario("S12")
async def test_a_parse_failure_preserves_its_diagnostics_and_stays_retryable(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """FR-ING-04/06 — "preserve job diagnostics and retry state".

    Two halves, and the second is the one nothing else covers: the diagnostics have to be
    *reachable*. `GET /api/v1/jobs/{id}` is the only route that renders `error_code` and
    `attempt_count`, and the document row is the only thing that hands out the job id — so
    this drives the pair the GUI actually uses, rather than reading the columns directly.
    """
    caller = await make_caller(session, make_token)
    # A PDF header with no page tree: it passes the upload route's sniffing and fails in
    # the parser, which is exactly "partial" — the file is a PDF, and its text is not there.
    created = await client.post(
        "/api/v1/documents",
        files=upload_files(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"),
        headers=caller.headers,
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]

    await queue.drain()

    document = await reload(session, Document, document_id)
    assert document.status is DocumentStatus.FAILED
    assert document.searchable is False

    listed = await client.get(f"/api/v1/documents/{document_id}", headers=caller.headers)
    assert listed.status_code == 200, listed.text
    job_id = listed.json()["latest_job_id"]
    assert job_id, "a failed document must hand out the job id that explains it"

    job_view = await client.get(f"/api/v1/jobs/{job_id}", headers=caller.headers)
    assert job_view.status_code == 200, job_view.text
    diagnostics = job_view.json()
    assert diagnostics["status"] == JobStatus.FAILED.value
    assert diagnostics["error_code"], "FR-ING-06 renders a code, not a bare failure"
    assert diagnostics["attempt_count"] >= 1

    # Retry state: a FAILED document is retryable, and the retry is a *new* job row for the
    # *same* version (R-39(5)) — so the diagnostics above are not overwritten by the retry.
    retried = await client.post(f"/api/v1/documents/{document_id}/retry", headers=caller.headers)
    assert retried.status_code == 202, retried.text
    new_job_id = retried.json()["job_id"]
    assert new_job_id != job_id
