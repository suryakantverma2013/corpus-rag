"""Documents list + get API tests (T-209, FR-KBM-03/04/09, R-40(5)/(6)).

Pure read surface, so there is no object storage and no queue here — only the shared
`client`/`session`/`make_token` fixtures, in the shape `test_jobs_api.py` established.

Two constraints from earlier tasks drive most of the odd-looking setup:

* **Every assertion is scoped to the caller this test minted** (T-109). The suite runs
  against the shared local database and nothing truncates it, so any assertion on a global
  count is only true on an empty database.
* **`created_at` is assigned explicitly wherever ordering matters.** `now()` is the
  *transaction* timestamp and `conftest`'s session never ends its outer transaction, so
  every row a test seeds otherwise carries a byte-identical timestamp — a tie is the
  guaranteed case here, not the rare one (T-108).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType, KBVisibility
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository

pytestmark = pytest.mark.usefixtures("patch_jwks")


# ---- helpers ----


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _document(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    knowledge_base_id: uuid.UUID | None = None,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    filename: str = "handbook.pdf",
    created_at: datetime | None = None,
    deleted_at: datetime | None = None,
    current_version: int = 1,
) -> Document:
    if knowledge_base_id is None:
        kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
        knowledge_base_id = kb.id
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=knowledge_base_id,
        tenant_id=DEFAULT_TENANT_ID,
        filename=filename,
        mime_type="application/pdf",
        storage_uri=f"file:///objects/{uuid.uuid4()}/original.pdf",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=2048,
        status=status,
        current_version=current_version,
        searchable=status is DocumentStatus.ACTIVE,
        page_count=58,
        chunk_count=12,
        deleted_at=deleted_at,
    )
    if created_at is not None:
        document.created_at = created_at
    session.add(document)
    await session.flush()
    return document


async def _job(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    document_version: int = 1,
    job_type: JobType = JobType.INGEST,
    status: JobStatus = JobStatus.SUCCEEDED,
    error_code: str | None = None,
    created_at: datetime | None = None,
) -> KnowledgeJob:
    job = KnowledgeJob(
        document_id=document_id,
        job_type=job_type,
        status=status,
        document_version=document_version,
        error_code=error_code,
        idempotency_key=f"ingest:{document_id}:v{document_version}:{uuid.uuid4().hex[:8]}",
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    await session.flush()
    return job


async def _chat_kb(session: AsyncSession, *, owner_id: uuid.UUID) -> tuple[Conversation, uuid.UUID]:
    conversation = Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title="Chat")
    session.add(conversation)
    await session.flush()
    kb = await KnowledgeBaseRepository(session).get_or_create_for_conversation(
        conversation.id, owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID
    )
    return conversation, kb.id


def _ids(body: list[dict]) -> list[str]:
    return [row["document_id"] for row in body]


# ---- list ----


async def test_list_returns_only_the_callers_live_documents(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    mine = await _document(session, owner_id=owner)
    theirs = await _document(session, owner_id=stranger)

    response = await client.get("/api/v1/documents", headers=headers)

    assert response.status_code == 200
    ids = _ids(response.json())
    assert ids == [str(mine.id)]
    assert str(theirs.id) not in ids


async def test_list_excludes_deleted_documents_but_shows_deleting_ones(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """FR-KBM-04 renders `Deleting`, and R-39(7) parks a dead-lettered purge there forever."""
    owner, headers = await _caller(session, make_token)
    pending = await _document(session, owner_id=owner, status=DocumentStatus.DELETE_PENDING)
    deleting = await _document(session, owner_id=owner, status=DocumentStatus.DELETING)
    gone = await _document(
        session,
        owner_id=owner,
        status=DocumentStatus.DELETED,
        deleted_at=datetime.now(UTC),
    )

    body = (await client.get("/api/v1/documents", headers=headers)).json()

    ids = _ids(body)
    assert str(pending.id) in ids
    assert str(deleting.id) in ids
    assert str(gone.id) not in ids


async def test_list_is_ordered_newest_first_with_a_deterministic_tiebreak(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """The T-108 proof: identical `created_at` must still yield a stable, total order."""
    owner, headers = await _caller(session, make_token)
    tie = datetime.now(UTC)
    docs = [await _document(session, owner_id=owner, created_at=tie) for _ in range(3)]
    # The tie condition must genuinely be present, or this test proves nothing.
    assert len({doc.created_at for doc in docs}) == 1

    first = _ids((await client.get("/api/v1/documents", headers=headers)).json())
    second = _ids((await client.get("/api/v1/documents", headers=headers)).json())

    assert first == sorted((str(doc.id) for doc in docs), reverse=True)
    assert first == second


async def test_list_paginates_by_limit_and_offset(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """Disjoint pages whose union is everything — what a missing tiebreak breaks."""
    owner, headers = await _caller(session, make_token)
    tie = datetime.now(UTC)
    docs = {str((await _document(session, owner_id=owner, created_at=tie)).id) for _ in range(3)}

    page1 = _ids((await client.get("/api/v1/documents?limit=2", headers=headers)).json())
    page2 = _ids((await client.get("/api/v1/documents?limit=2&offset=2", headers=headers)).json())

    assert len(page1) == 2
    assert len(page2) == 1
    assert set(page1).isdisjoint(page2)
    assert set(page1) | set(page2) == docs


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1"])
async def test_list_rejects_an_out_of_range_page(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
    query: str,
) -> None:
    _, headers = await _caller(session, make_token)
    response = await client.get(f"/api/v1/documents?{query}", headers=headers)
    assert response.status_code == 422


async def test_list_filters_by_scope(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, headers = await _caller(session, make_token)
    global_doc = await _document(session, owner_id=owner)
    conversation, chat_kb_id = await _chat_kb(session, owner_id=owner)
    chat_doc = await _document(session, owner_id=owner, knowledge_base_id=chat_kb_id)

    global_body = (await client.get("/api/v1/documents?scope=global", headers=headers)).json()
    chat_body = (
        await client.get(
            f"/api/v1/documents?scope=chat&conversation_id={conversation.id}", headers=headers
        )
    ).json()

    assert _ids(global_body) == [str(global_doc.id)]
    assert global_body[0]["scope"] == "global"
    assert global_body[0]["conversation_id"] is None
    assert _ids(chat_body) == [str(chat_doc.id)]
    assert chat_body[0]["scope"] == "chat"
    assert chat_body[0]["conversation_id"] == str(conversation.id)


async def test_list_of_chat_scope_without_a_conversation_id_is_400(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    response = await client.get("/api/v1/documents?scope=chat", headers=headers)
    assert response.status_code == 400


async def test_list_of_a_foreign_conversation_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    conversation, _ = await _chat_kb(session, owner_id=stranger)

    response = await client.get(
        f"/api/v1/documents?scope=chat&conversation_id={conversation.id}", headers=headers
    )

    assert response.status_code == 404


async def test_list_does_not_create_a_knowledge_base(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """A `GET` that INSERTs takes write locks for a read and, under a session that never
    commits, leaves a row that appears to exist and then does not. The app shares this
    test's session, so a merely-flushed KB *is* visible here — this genuinely catches
    `get_or_create_default` being reached from the list handler."""
    owner, headers = await _caller(session, make_token)

    response = await client.get("/api/v1/documents?scope=global", headers=headers)

    assert response.status_code == 200
    assert response.json() == []
    assert await KnowledgeBaseRepository(session).get_default(owner) is None
    stmt = select(KnowledgeBase).where(KnowledgeBase.owner_id == owner)
    assert (await session.scalars(stmt)).all() == []


async def test_list_filters_by_status(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, headers = await _caller(session, make_token)
    failed = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    await _document(session, owner_id=owner, status=DocumentStatus.ACTIVE)

    body = (await client.get("/api/v1/documents?status=FAILED", headers=headers)).json()

    assert _ids(body) == [str(failed.id)]


async def test_list_carries_the_newest_job_id_and_error_code(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-40(6). `created_at` is assigned explicitly — in one transaction both jobs tie."""
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    now = datetime.now(UTC)
    await _job(session, document_id=document.id, created_at=now - timedelta(minutes=5))
    newest = await _job(
        session,
        document_id=document.id,
        document_version=2,
        status=JobStatus.FAILED,
        error_code="PARSE_FAILED",
        created_at=now,
    )

    row = (await client.get("/api/v1/documents", headers=headers)).json()[0]

    assert row["latest_job_id"] == str(newest.id)
    assert row["latest_job_error_code"] == "PARSE_FAILED"
    assert row["latest_job_document_version"] == 2


async def test_list_returns_documents_that_have_no_job_at_all(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """The `LEFT JOIN LATERAL … ON true` proof — an inner join yields an empty page, which
    reads like an authorization bug rather than a join bug."""
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)

    row = (await client.get("/api/v1/documents", headers=headers)).json()[0]

    assert row["document_id"] == str(document.id)
    assert row["latest_job_id"] is None
    assert row["latest_job_error_code"] is None
    assert row["latest_job_document_version"] is None


async def test_the_listing_query_does_not_fan_out_per_document(
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """The N+1 guard the lateral join exists to enforce (R-40(6))."""
    owner, _ = await _caller(session, make_token)
    for _ in range(5):
        document = await _document(session, owner_id=owner)
        await _job(session, document_id=document.id)

    statements: list[str] = []
    bind = session.get_bind()

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", _record)
    try:
        listings = await DocumentRepository(session).list_for_owner(owner_id=owner)
    finally:
        event.remove(bind, "before_cursor_execute", _record)

    assert len(listings) == 5
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, selects


async def test_list_never_exposes_the_storage_uri_or_checksum(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-31(4)'s revisit trigger stays untripped, and catches the `model_validate(...,
    from_attributes=True)` shortcut a later contributor will reach for."""
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)

    response = await client.get("/api/v1/documents", headers=headers)

    row = response.json()[0]
    assert "storage_uri" not in row
    assert "checksum_sha256" not in row
    raw = response.text
    assert document.storage_uri not in raw
    assert document.checksum_sha256 not in raw


async def test_list_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/documents")).status_code == 401


# ---- get ----


async def test_get_returns_the_document_metadata(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, filename="policy.pdf")
    job = await _job(session, document_id=document.id)

    body = (await client.get(f"/api/v1/documents/{document.id}", headers=headers)).json()

    # The FR-KBM-09 columns: Filename · Status · Version · Chunks · Last updated.
    assert body["filename"] == "policy.pdf"
    assert body["status"] == DocumentStatus.ACTIVE
    assert body["current_version"] == 1
    assert body["chunk_count"] == 12
    assert body["updated_at"] is not None
    # Plus what FR-KBM-04's row and the scope sections need.
    assert body["page_count"] == 58
    assert body["size_bytes"] == 2048
    assert body["searchable"] is True
    assert body["scope"] == "global"
    assert body["latest_job_id"] == str(job.id)
    assert body["deleted_at"] is None


async def test_get_of_a_foreign_document_is_404_not_403(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=stranger)

    response = await client.get(f"/api/v1/documents/{document.id}", headers=headers)

    assert response.status_code == 404


async def test_an_admin_may_get_another_users_document(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    document = await _document(session, owner_id=owner)

    response = await client.get(f"/api/v1/documents/{document.id}", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["document_id"] == str(document.id)


async def test_get_of_an_unknown_document_is_404(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    response = await client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404


async def test_get_returns_a_deleted_document_with_its_terminal_state(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """The deliberate list/get asymmetry (R-40(5)): a client polling after `DELETE`'s 202
    must see `DELETED`, not a 404 it cannot tell apart from a wrong id."""
    owner, headers = await _caller(session, make_token)
    document = await _document(
        session,
        owner_id=owner,
        status=DocumentStatus.DELETED,
        deleted_at=datetime.now(UTC),
    )

    body = (await client.get(f"/api/v1/documents/{document.id}", headers=headers)).json()

    assert body["status"] == DocumentStatus.DELETED
    assert body["deleted_at"] is not None
    assert _ids((await client.get("/api/v1/documents", headers=headers)).json()) == []


async def test_get_exposes_no_storage_uri_and_no_chunk_id(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-31(4) metadata-only, plus R-36(6)(b): nothing here may resolve a citation by
    chunk id — a replaced document's historical chunk ids dangle by design."""
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)

    response = await client.get(f"/api/v1/documents/{document.id}", headers=headers)

    body = response.json()
    assert "storage_uri" not in body
    assert document.storage_uri not in response.text
    assert not [key for key in body if "chunk" in key and key != "chunk_count"]


async def test_get_requires_authentication(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)
    assert (await client.get(f"/api/v1/documents/{document.id}")).status_code == 401


async def test_scope_visibility_maps_from_the_knowledge_base(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """`scope` is derived from `KBVisibility`, not stored on the document."""
    owner, headers = await _caller(session, make_token)
    _, chat_kb_id = await _chat_kb(session, owner_id=owner)
    document = await _document(session, owner_id=owner, knowledge_base_id=chat_kb_id)

    body = (await client.get(f"/api/v1/documents/{document.id}", headers=headers)).json()

    kb = await session.get(KnowledgeBase, chat_kb_id)
    assert kb is not None and kb.visibility is KBVisibility.CONVERSATION
    assert body["scope"] == "chat"


# ---- the R-24 processing lock (FR-STA-02 / FR-ORC-04, R-43) ----


@pytest.fixture
def offline_clients(app):  # noqa: ANN001, ANN201 — the conftest FastAPI app
    """Keep the gated routes off the real broker and object store.

    FastAPI resolves `JobQueueDep` / `ObjectStorageDep` **before** the handler runs, so a
    request that is about to be refused with a 409 would still build — and module-globally
    cache — a live arq Redis pool bound to this test's event loop. `test_job_queue`'s own
    `close_job_queue()` then fails with "Event loop is closed" much later in the run. Same
    `dependency_overrides` shape `test_upload` / `test_deletion` / `test_replace` use.
    """
    from app.services.jobs import NullJobQueue, get_job_queue
    from app.services.object_storage import get_object_storage

    class _Storage:
        """Never called — the gate refuses before any handler body runs."""

    app.dependency_overrides[get_job_queue] = NullJobQueue
    app.dependency_overrides[get_object_storage] = _Storage
    yield
    app.dependency_overrides.pop(get_job_queue, None)
    app.dependency_overrides.pop(get_object_storage, None)


async def _hold_lock(
    session: AsyncSession, *, owner_id: uuid.UUID, expires_in: timedelta = timedelta(minutes=3)
) -> None:
    """Publish a gate for `owner_id` directly, as the graph's `lock` node would."""
    session.add(
        ProcessingLock(
            owner_id=owner_id,
            conversation_id=None,
            token=uuid.uuid4().hex,
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + expires_in,
        )
    )
    await session.flush()


def _upload_file() -> dict:
    return {"file": ("notes.md", b"# Notes\n\nSome content.\n", "text/markdown")}


@pytest.mark.parametrize("verb", ["upload", "delete", "retry", "replace"])
@pytest.mark.usefixtures("offline_clients")
async def test_the_four_mutating_verbs_are_gated_while_a_response_generates(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
    verb: str,
) -> None:
    """FR-STA-02 names exactly these four (R-24), and R-43 answers all four with `409`.

    `409` rather than `429` because these routes already carry a slowapi limit and the
    client could not tell a throttle from a busy chat; rather than `423` because what is
    locked is the caller's session, not the document.

    **The body is object-shaped and carries `error_code` (R-71(1), closing OI-31).** These
    routes answer `409` for other reasons too — `NotRetryableError`, `NotReplaceableError`,
    `DuplicateChecksumError` — and the GUI *reconciles* this one rather than rendering it as an
    error, so it has to tell them apart. Asserting the code rather than a substring of the copy
    is also what stops this test pinning a `# TBD(§8.4)` string that is expected to change.
    """
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    await _hold_lock(session, owner_id=owner)

    if verb == "upload":
        response = await client.post("/api/v1/documents", headers=headers, files=_upload_file())
    elif verb == "delete":
        response = await client.delete(f"/api/v1/documents/{document.id}", headers=headers)
    elif verb == "retry":
        response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)
    else:
        response = await client.post(
            f"/api/v1/documents/{document.id}/replace", headers=headers, files=_upload_file()
        )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "PROCESSING_LOCKED"
    assert "paused" in detail["message"]


@pytest.mark.usefixtures("offline_clients")
async def test_an_expired_gate_does_not_block(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """`expires_at` is the crash release: a row left by a killed run must gate nothing."""
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    await _hold_lock(session, owner_id=owner, expires_in=timedelta(minutes=-1))

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    assert response.status_code == 202, response.text


@pytest.mark.usefixtures("offline_clients")
async def test_another_users_turn_does_not_gate_me(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """R-24 rescoped FR-STA-02 away from a global freeze; this is that, asserted."""
    owner, headers = await _caller(session, make_token)
    stranger, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    await _hold_lock(session, owner_id=stranger)

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    assert response.status_code == 202, response.text


async def test_read_routes_are_never_gated(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """FR-STA-02 names four *mutating* verbs.

    A knowledge-base modal that stopped listing documents while the user chatted would be a
    spectacular misreading of it — and `GET /events` is how T-508 watches ingestion, which
    is exactly the thing a user is most likely to be doing while a turn runs.
    """
    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)
    await _hold_lock(session, owner_id=owner)

    assert (await client.get("/api/v1/documents", headers=headers)).status_code == 200
    assert (
        await client.get(f"/api/v1/documents/{document.id}", headers=headers)
    ).status_code == 200


@pytest.mark.usefixtures("offline_clients")
async def test_the_gate_precedes_every_side_effect(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    """A refused upload must not create the on-demand knowledge base it would have used.

    `_resolve_knowledge_base` creates the GLOBAL KB on first upload, and `replace_document`
    buffers 50 MB before it authorizes — so the guard runs first in both, or a gated request
    still costs the server the work it was refused.
    """
    owner, headers = await _caller(session, make_token)
    await _hold_lock(session, owner_id=owner)

    response = await client.post("/api/v1/documents", headers=headers, files=_upload_file())

    assert response.status_code == 409
    kbs = (
        await session.scalars(select(KnowledgeBase).where(KnowledgeBase.owner_id == owner))
    ).all()
    assert kbs == [], "a refused upload still created a knowledge base"


@pytest.mark.usefixtures("offline_clients")
async def test_the_kill_switch_disables_enforcement_only(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GRAPH_LOCK_ENFORCED=false` is for diagnosis, never a handling path for a bug."""
    from app.config import get_settings

    owner, headers = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)
    await _hold_lock(session, owner_id=owner)

    settings = get_settings()
    monkeypatch.setattr(settings.graph, "lock_enforced", False)

    response = await client.post(f"/api/v1/documents/{document.id}/retry", headers=headers)

    assert response.status_code == 202, response.text
