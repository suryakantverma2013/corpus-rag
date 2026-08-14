"""§11 rows 4, 7 and 8 — what is in scope for the next query, and what is not.

All three are claims about **the very next query**: FR-RET-04 puts the authorization
predicate *in the query* rather than in application code, and NFR-SEC-06 draws the
consequence — "loss of document permission or document deletion excludes it from the next
query immediately". There is no cache to invalidate, so the property is testable by changing
one fact and asking again.

Rows 7 and 8 do not describe this system literally; R-76 (§8.65) rules the reading, and each
test states the one it relies on.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentStatus
from app.db.models.document import Document
from tests.scenarios import scenario
from tests.scenarios.conftest import (
    DrainingQueue,
    chunks_of,
    make_caller,
    pdf,
    reload,
    sse_frames,
    upload_files,
)

pytestmark = pytest.mark.usefixtures("patch_jwks")

#: The lexical anchor the corpus carries and the question asks for. Distinctive so a hit is
#: attributable to this document rather than to whatever else is in the shared dev database.
ANCHOR = "The perihelion precession of Mercury is forty-three arcseconds per century."
QUESTION = "What is the perihelion precession of Mercury?"


async def _ingested_document(client, caller, queue: DrainingQueue, text: str = ANCHOR) -> str:
    """Upload through the route and run the real worker. Returns the document id."""
    created = await client.post(
        "/api/v1/documents", files=upload_files(pdf(text)), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    await queue.drain()
    return created.json()["document_id"]


async def _conversation(client, caller) -> str:
    created = await client.post(
        "/api/v1/conversations", json={"title": "Scenario"}, headers=caller.headers
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _ask(client, caller, conversation_id: str, query: str = QUESTION) -> dict:
    """Post one turn and return `{outcome, text, citations}` from the SSE frames.

    The answer arrives as FR-MSG-06 `segs`, nested at `data.message.segs` — the `message`
    frame's payload is `MessageFrameData{outcome, error_code, message}`, so the segments are
    one level deeper than the frame. Text segments carry `text`; citation segments carry
    `isCite: true` and address a passage by **`chunkId`**. There is no document id on the
    wire, which is why the tests below resolve chunk ids through the database.
    """
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"query": query},
        headers=caller.headers,
    )
    assert response.status_code == 200, response.text
    frames = await sse_frames(response)
    kinds = [name for name, _ in frames]
    assert kinds[-1] == "done", kinds
    frame = next((data for name, data in frames if name == "message"), {})
    message = frame.get("message") or {}
    segs = message.get("segs", [])
    return {
        "outcome": frames[-1][1].get("outcome") or frame.get("outcome"),
        "error_code": frame.get("error_code"),
        "text": "".join(seg.get("text", "") for seg in segs if not seg.get("isCite")),
        "citations": [seg for seg in segs if seg.get("isCite")],
        "message": message,
    }


# --- row 4: query during deletion -----------------------------------------------------


@scenario("S04")
async def test_a_deleted_document_leaves_the_scope_on_the_very_next_query(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """FR-ING-05 + FR-RET-04, across the full route → worker → route path.

    The interleaving is deliberate: the `DELETE` is issued and the purge job is **not**
    drained, so the chunk rows are still physically present when the second question is
    asked. That is what makes the assertion sharp — the document leaves the scope because
    `documents.searchable` went false in the synchronous half of FR-ING-05, **not** because
    its rows had already been collected.

    R-62 dropped `document_chunks.is_active`, so `documents.searchable`/`deleted_at` are the
    only things carrying this. Remove either from `_access_predicates` and the second turn
    answers from a document the user has deleted.
    """
    caller = await make_caller(session, make_token)
    document_id = await _ingested_document(client, caller, queue)
    conversation_id = await _conversation(client, caller)

    own_chunks = {str(row.id) for row in await chunks_of(session, document_id)}
    grounded = await _ask(client, caller, conversation_id)
    assert grounded["outcome"] == "answered", grounded
    assert grounded["citations"], "the corpus should have grounded this turn"
    assert {citation["chunkId"] for citation in grounded["citations"]} <= own_chunks, (
        "the turn cited a passage that is not this document's"
    )

    deleted = await client.delete(f"/api/v1/documents/{document_id}", headers=caller.headers)
    assert deleted.status_code == 202, deleted.text
    assert queue.pending, "the purge job is deliberately left unrun"

    # The rows are still there. The exclusion must not depend on their removal.
    assert await chunks_of(session, document_id), (
        "the purge has not run yet; if these are gone the test proves the wrong thing"
    )

    after = await _ask(client, caller, conversation_id)
    assert after["outcome"] == "abstained", after
    assert not after["citations"], (
        "a deleted document answered the very next query — FR-RET-04's predicate is not "
        "being applied in the query"
    )


# --- row 7: embedding model changes ---------------------------------------------------


@scenario("S07")
async def test_an_embedding_model_change_re_embeds_the_whole_document(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
) -> None:
    """FR-ING-03, under R-76's reading of "controlled re-embedding job".

    **`JobType` has exactly `INGEST` and `DELETE` — there is no re-embedding job.** So the
    *control* §11 asks for is not a job type: it is `embedding_fingerprint`, which folds in
    `embedding_model` (`chunker.compute_embedding_fingerprint`), so a model change
    invalidates every fingerprint and the next ingest of that document rebuilds it whole.

    The test asserts both halves, and **the first half is the gap, asserted rather than
    described**: an `ACTIVE` document cannot be re-ingested at all. FR-ING-04's short-circuit
    ("an `ACTIVE` document short-circuits ingestion") means a same-version job against a
    healthy document does nothing — so after a model change there is no reachable way to
    re-embed a healthy corpus. `/retry` requires `FAILED`, and `/replace` with identical
    bytes answers `200 duplicate` and queues nothing.

    The second half drives the one path that does exist — FR-ING-04's retry — and shows the
    fingerprint doing the work §11 credits to a job. The missing operator trigger is a real
    gap; it is on the board as its own task rather than papered over here.
    """
    from app.services.embeddings import FakeEmbeddingClient

    caller = await make_caller(session, make_token)
    # The exact bytes, kept: PyMuPDF stamps a fresh document id on every build, so
    # `pdf(ANCHOR)` twice is *not* the same file and would be a genuine replace.
    payload = pdf(ANCHOR)
    created = await client.post(
        "/api/v1/documents", files=upload_files(payload), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]
    await queue.drain()

    before = await chunks_of(session, document_id)
    assert before
    first_fingerprints = {row.embedding_fingerprint for row in before}
    document = await reload(session, Document, document_id)
    assert document.status is DocumentStatus.ACTIVE

    # The model is read off the embedder the worker is handed (`deps.embedder.model`), which
    # is what reaches `compute_embedding_fingerprint`.
    new_model = FakeEmbeddingClient(model="text-embedding-3-small")

    # Half one: a healthy document is unreachable. Re-driving the *same* version short-
    # circuits, so the model change cannot take effect however the job is delivered.
    replaced = await client.post(
        f"/api/v1/documents/{document_id}/replace",
        files=upload_files(payload),
        headers=caller.headers,
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["duplicate"] is True
    assert replaced.json()["job_id"] is None
    assert not queue.pending, "identical bytes must queue nothing"

    retry_while_active = await client.post(
        f"/api/v1/documents/{document_id}/retry", headers=caller.headers
    )
    assert retry_while_active.status_code == 409, (
        "retry is FAILED-only (R-39(5)); if this ever becomes 202 the gap below has closed "
        "and this test should be re-pointed at the new route"
    )

    assert {row.embedding_fingerprint for row in await chunks_of(session, document_id)} == (
        first_fingerprints
    ), "nothing should have re-embedded: there is no route that re-drives an ACTIVE document"

    # Half two: the one reachable re-ingest — FR-ING-04's retry of a failed document. This is
    # what a real operator would be left with after changing the model.
    document = await reload(session, Document, document_id)
    document.status = DocumentStatus.FAILED
    document.searchable = False
    await session.commit()

    retried = await client.post(f"/api/v1/documents/{document_id}/retry", headers=caller.headers)
    assert retried.status_code == 202, retried.text
    await queue.drain(embedder_override=new_model)

    after = await chunks_of(session, document_id)
    assert after
    second_fingerprints = {row.embedding_fingerprint for row in after}

    assert not (first_fingerprints & second_fingerprints), (
        "a model change must invalidate every fingerprint — an overlap means a vector "
        "produced by the old model would be carried forward and served as if it were new"
    )
    assert new_model.embedded_inputs >= len(after), (
        "carry-forward matched something it should not have: every chunk must be re-embedded"
    )

    # Never both models at once: retrieval joins on `document_version = current_version`, so
    # a stale-model vector is unreachable the moment the swap commits.
    document = await reload(session, Document, document_id)
    assert {row.document_version for row in after} == {document.current_version}


# --- row 8: user loses document permission --------------------------------------------


@scenario("S08")
async def test_losing_access_to_a_document_excludes_it_from_the_next_query(
    client, session: AsyncSession, make_token: Callable[..., str], queue: DrainingQueue
) -> None:
    """FR-RET-04 / NFR-SEC-06, under R-76's reading of "loses document permission".

    Corpus has **no sharing or ACL model** — OI-15 fixed knowledge-base scope as
    global-per-user, so a document has exactly one owner and permission is never granted,
    hence never revoked. The property §11 is protecting is nonetheless real and is the one
    asserted here: **the authorization predicate is evaluated in the query, from live
    context, on every turn** — so any change that moves a document out of the caller's scope
    takes effect on the very next question, with no cache to invalidate and no restart.

    That it is *live* rather than *checkpointed* is the load-bearing half: `_retrieval_filter`
    derives from `runtime.context`, and R-42(3) keeps authorization out of the checkpoint
    precisely so a resumed run re-authorizes against the current principal.

    **Boundary with T-602**, which owns "permission-loss mid-session" in the authz matrix:
    T-602 owns *who may call what* — the status codes when a principal's roles or account
    state change. This owns exactly one claim, about the retrieval predicate. Neither
    restates the other.
    """
    owner = await make_caller(session, make_token)
    document_id = await _ingested_document(client, owner, queue)
    conversation_id = await _conversation(client, owner)

    grounded = await _ask(client, owner, conversation_id)
    assert grounded["outcome"] == "answered", grounded
    assert grounded["citations"], "the corpus should have grounded this turn"

    # The document moves out of the caller's scope mid-session — same user, same
    # conversation, same question, one fact changed.
    stranger = await make_caller(session, make_token)
    document = await reload(session, Document, document_id)
    document.owner_id = stranger.id
    await session.commit()

    after = await _ask(client, owner, conversation_id)
    assert after["outcome"] == "abstained", after
    assert not after["citations"], (
        "a document the caller can no longer reach answered the very next query — the "
        "FR-RET-04 predicate is being applied somewhere other than in the query"
    )

    persisted = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=owner.headers
    )
    assert persisted.status_code == 200, persisted.text
    last = persisted.json()[-1]
    assert not [seg for seg in last.get("segs", []) if seg.get("isCite")], (
        "the persisted transcript must not carry a citation to a document that was out of "
        "scope when the turn ran"
    )
