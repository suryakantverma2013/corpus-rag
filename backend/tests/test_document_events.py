"""Live document-status channel tests (T-210, FR-KBM-09, ruling R-41 §8.22).

Two levels, because the two halves fail differently. The engine
(`app.services.document_events`) is driven directly with `max_ticks`, so the diffing and
the `stalled` derivation are tested without a socket or a clock; the route's rejections
go through the ASGI client, and its framing and slot accounting are driven by calling the
handler directly — see `_open_stream` for why an endless stream cannot go through
`httpx.ASGITransport` at all.

Same constraints as `test_documents_api.py`: every assertion is scoped to the caller the
test minted (T-109 — the suite runs against a shared database that nothing truncates),
and `updated_at` is written explicitly wherever staleness matters, since `now()` is the
*transaction* timestamp and the fixture's outer transaction never ends (T-108).

The engine tests take `db_connection` rather than `session` because the engine opens its
own session per tick (R-41(7)) — see `_factory`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api.documents import StreamScope, _frame, hold_stream_slot, stream_documents
from app.config import Settings, SseSettings, get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.services import document_events
from app.services.document_events import (
    DocumentChanged,
    DocumentRemoved,
    DocumentState,
    Snapshot,
    stream_document_events,
)

pytestmark = pytest.mark.usefixtures("patch_jwks")

_STREAM_URL = "/api/v1/documents/events"


# ---- helpers ----


async def _caller(
    session: AsyncSession, make_token: Callable[..., str]
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=('user',))}"}


async def _document(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    filename: str = "handbook.pdf",
) -> Document:
    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename=filename,
        mime_type="application/pdf",
        storage_uri=f"file:///objects/{uuid.uuid4()}/original.pdf",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=2048,
        status=status,
        current_version=1,
        searchable=status is DocumentStatus.ACTIVE,
        page_count=58,
        chunk_count=12,
    )
    session.add(document)
    await session.flush()
    return document


async def _age(session: AsyncSession, document: Document, *, seconds: float) -> None:
    """Backdate `updated_at` past the stall threshold.

    A Core `UPDATE` with an explicit value, not an ORM attribute write: `TimestampMixin`
    sets `onupdate=func.now()`, so an ORM flush would stamp the row straight back to the
    present and the test would silently assert nothing.
    """
    await session.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(updated_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )


def _factory(connection: AsyncConnection) -> Callable[[], AsyncSession]:
    """Stand in for `async_sessionmaker`, mirroring production.

    Each call opens a **real** short-lived session, as the engine does per tick, bound to
    the fixture's connection so it joins the transaction the test rolls back. Handing the
    engine the test's own long-lived session instead would give it a populated identity
    map, and the ORM would then return the attribute values that session already held
    rather than the ones the SELECT just read — the test would pass while asserting
    nothing.
    """
    return lambda: AsyncSession(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


def _events(
    connection: AsyncConnection,
    *,
    owner_id: uuid.UUID,
    ticks: int,
    stall_after: float = 3600.0,
):  # noqa: ANN201 — the engine's AsyncIterator
    return stream_document_events(
        _factory(connection),  # type: ignore[arg-type]
        owner_id=owner_id,
        poll_interval=0.0,
        stall_after=stall_after,
        max_ticks=ticks,
    )


async def _collect(
    connection: AsyncConnection,
    *,
    owner_id: uuid.UUID,
    ticks: int,
    stall_after: float = 3600.0,
) -> list[object]:
    stream = _events(connection, owner_id=owner_id, ticks=ticks, stall_after=stall_after)
    return [event async for event in stream]


def _fast_settings(**sse: object) -> Settings:
    """Real settings with the SSE knobs turned down so a test never waits on a poll."""
    base = get_settings()
    return base.model_copy(
        update={
            "sse": SseSettings(
                poll_interval_seconds=0.01,
                **sse,  # type: ignore[arg-type]
            )
        }
    )


async def _states(connection: AsyncConnection, *, owner_id: uuid.UUID) -> tuple[DocumentState, ...]:
    """The engine's view of one caller's documents, for framing assertions."""
    snapshot = (await _collect(connection, owner_id=owner_id, ticks=1))[0]
    assert isinstance(snapshot, Snapshot)
    return snapshot.documents


async def _open_stream(
    session: AsyncSession, connection: AsyncConnection, *, owner_id: uuid.UUID
) -> AsyncIterator[object]:
    """Call the route handler directly and hand back its frame generator.

    **Why not `client.stream(...)`.** `httpx.ASGITransport` (0.28.1) accumulates the whole
    response body and only returns once the ASGI app has finished — see `send()` in
    `httpx/_transports/asgi.py`, which appends to `body_parts` and waits on
    `response_complete`. An SSE stream never finishes, so any test that drove this over
    the ASGI transport would hang rather than fail.

    Since T-405 the handler **is** the generator (`fastapi.sse`), so this yields frames
    directly rather than a response object. Its scope and its stream slot now arrive as
    resolved dependencies — which is exactly the point of that refactor, and is why the slot
    accounting is tested against `hold_stream_slot` itself below.
    """
    user = await UserRepository(session).get(owner_id)
    assert user is not None
    return stream_documents(
        scope_=StreamScope(knowledge_base_id=None),
        _slot=None,
        user=user,
        sessionmaker=_factory(connection),  # type: ignore[arg-type]
        settings=_fast_settings(),
    )


# ---- the engine: diffing ----


async def test_first_tick_emits_a_snapshot_of_the_callers_documents(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner)

    events = await _collect(db_connection, owner_id=owner, ticks=1)

    assert len(events) == 1
    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert [state.document_id for state in snapshot.documents] == [document.id]


async def test_an_empty_knowledge_base_still_snapshots(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """A connect always answers, so the GUI can tell "no documents" from "not connected"."""
    owner, _ = await _caller(session, make_token)

    events = await _collect(db_connection, owner_id=owner, ticks=1)

    assert events == [Snapshot(documents=())]


async def test_an_unchanged_tick_emits_nothing(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    await _document(session, owner_id=owner)

    events = await _collect(db_connection, owner_id=owner, ticks=3)

    assert len(events) == 1
    assert isinstance(events[0], Snapshot)


async def test_a_status_change_emits_exactly_one_document_event(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.PARSING)

    stream = _events(db_connection, owner_id=owner, ticks=3)
    snapshot = await anext(stream)
    assert isinstance(snapshot, Snapshot)

    document.status = DocumentStatus.EMBEDDING
    await session.flush()

    changed = await anext(stream)
    assert isinstance(changed, DocumentChanged)
    assert changed.state.document_id == document.id
    assert changed.state.listing.document.status is DocumentStatus.EMBEDDING

    # The third tick has nothing to report, so the generator ends rather than yielding.
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_a_new_job_row_emits_the_job_back_reference(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """R-40(6)'s job id is part of the rendered row, so it must move the stream too."""
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.FAILED)

    stream = _events(db_connection, owner_id=owner, ticks=2)
    await anext(stream)

    job = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.DEAD_LETTER,
        document_version=1,
        error_code="OBJECT_STORAGE_UNAVAILABLE",
        idempotency_key=f"ingest:{document.id}:v1:{uuid.uuid4().hex[:8]}",
    )
    session.add(job)
    await session.flush()

    changed = await anext(stream)
    assert isinstance(changed, DocumentChanged)
    assert changed.state.listing.latest_job_id == job.id
    assert changed.state.listing.latest_job_error_code == "OBJECT_STORAGE_UNAVAILABLE"


async def test_a_tombstoned_document_emits_removed(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """A deletion cannot transition into a visible state — it just leaves the set."""
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.DELETING)

    stream = _events(db_connection, owner_id=owner, ticks=2)
    await anext(stream)

    document.status = DocumentStatus.DELETED
    document.deleted_at = datetime.now(UTC)
    await session.flush()

    removed = await anext(stream)
    assert removed == DocumentRemoved(document_id=document.id)


async def test_another_users_documents_never_appear(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """NFR-SEC-06: owner scoping is a query predicate, not a filter this loop applies."""
    owner, _ = await _caller(session, make_token)
    other, _ = await _caller(session, make_token)
    await _document(session, owner_id=other, filename="theirs.pdf")
    mine = await _document(session, owner_id=owner, filename="mine.pdf")

    events = await _collect(db_connection, owner_id=owner, ticks=1)

    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert [state.document_id for state in snapshot.documents] == [mine.id]


# ---- the engine: the `stalled` derivation (R-41(5), T-212) ----


async def test_a_quiet_in_flight_document_is_stalled(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.EMBEDDING)
    await _age(session, document, seconds=1200)

    events = await _collect(db_connection, owner_id=owner, ticks=1, stall_after=910.0)

    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert snapshot.documents[0].stalled is True
    # The status is untouched: no twelfth DocumentStatus, no ninth FR-KBM-04 label.
    assert snapshot.documents[0].listing.document.status is DocumentStatus.EMBEDDING


async def test_a_recently_touched_in_flight_document_is_not_stalled(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    await _document(session, owner_id=owner, status=DocumentStatus.EMBEDDING)

    events = await _collect(db_connection, owner_id=owner, ticks=1, stall_after=910.0)

    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert snapshot.documents[0].stalled is False


@pytest.mark.parametrize("status", [DocumentStatus.DELETE_PENDING, DocumentStatus.DELETING])
async def test_a_deleting_document_is_never_stalled(
    session: AsyncSession,
    db_connection: AsyncConnection,
    make_token: Callable[..., str],
    status: DocumentStatus,
) -> None:
    """R-39(7): a dead-lettered purge parks here deliberately and must keep reading
    `Deleting` however long it persists — marking it stalled would contradict the ruling
    on exactly the documents the ruling was written for."""
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=status)
    await _age(session, document, seconds=86_400)

    events = await _collect(db_connection, owner_id=owner, ticks=1, stall_after=1.0)

    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert snapshot.documents[0].stalled is False


@pytest.mark.parametrize("status", [DocumentStatus.ACTIVE, DocumentStatus.FAILED])
async def test_terminal_documents_are_never_stalled(
    session: AsyncSession,
    db_connection: AsyncConnection,
    make_token: Callable[..., str],
    status: DocumentStatus,
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=status)
    await _age(session, document, seconds=86_400)

    events = await _collect(db_connection, owner_id=owner, ticks=1, stall_after=1.0)

    snapshot = events[0]
    assert isinstance(snapshot, Snapshot)
    assert snapshot.documents[0].stalled is False


async def test_flipping_to_stalled_emits_an_event_though_no_row_changed(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """`stalled` is derived, so time alone must be able to move the stream (R-41(5))."""
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.INDEXING)

    stream = _events(db_connection, owner_id=owner, ticks=2, stall_after=900.0)
    snapshot = await anext(stream)
    assert isinstance(snapshot, Snapshot)
    assert snapshot.documents[0].stalled is False

    await _age(session, document, seconds=1200)

    changed = await anext(stream)
    assert isinstance(changed, DocumentChanged)
    assert changed.state.stalled is True


# ---- the route ----


async def test_the_stream_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get(_STREAM_URL)
    assert response.status_code == 401


async def test_chat_scope_without_a_conversation_is_400(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _caller(session, make_token)
    response = await client.get(_STREAM_URL, params={"scope": "chat"}, headers=headers)
    assert response.status_code == 400


async def test_a_foreign_conversation_is_404(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    _, headers = await _caller(session, make_token)
    other, _ = await _caller(session, make_token)
    conversation = Conversation(owner_id=other, tenant_id=DEFAULT_TENANT_ID, title="Theirs")
    session.add(conversation)
    await session.flush()

    response = await client.get(
        _STREAM_URL,
        params={"scope": "chat", "conversation_id": str(conversation.id)},
        headers=headers,
    )
    assert response.status_code == 404


async def test_the_snapshot_frame_carries_the_metadata_only_dto(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """R-31(4)/R-36(6)(b): nothing here may carry bytes, point at them, or name a chunk."""
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.EMBEDDING)

    frame = _frame(Snapshot(documents=await _states(db_connection, owner_id=owner)))

    assert frame.event == "snapshot"
    rows = json.loads(frame.to_event().data.model_dump_json())["data"]
    assert [row["document_id"] for row in rows] == [str(document.id)]
    assert rows[0]["stalled"] is False
    assert rows[0]["status"] == "EMBEDDING"
    for forbidden in ("storage_uri", "checksum_sha256", "chunk_id", "chunk_ids"):
        assert forbidden not in rows[0]


async def test_a_change_is_framed_as_one_document_event(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    owner, _ = await _caller(session, make_token)
    document = await _document(session, owner_id=owner, status=DocumentStatus.PARSING)
    states = await _states(db_connection, owner_id=owner)

    frame = _frame(DocumentChanged(state=states[0]))

    assert frame.event == "document"
    row = json.loads(frame.to_event().data.model_dump_json())["data"]
    assert row["document_id"] == str(document.id)
    assert row["status"] == "PARSING"
    assert row["stalled"] is False


def test_a_removal_is_framed_as_an_id_only() -> None:
    document_id = uuid.uuid4()

    frame = _frame(DocumentRemoved(document_id=document_id))

    assert frame.event == "removed"
    assert json.loads(frame.to_event().data.model_dump_json()) == {
        "event": "removed",
        "data": {"document_id": str(document_id)},
    }


async def test_exceeding_the_stream_cap_is_429(
    app, client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    owner, headers = await _caller(session, make_token)
    # `lambda:`, not `_fast_settings` itself — FastAPI introspects an override's signature,
    # and `**sse` would be read as a required query parameter (a 422 on every request).
    app.dependency_overrides[get_settings] = lambda: _fast_settings(max_streams_per_user=1)
    document_events.registry.acquire(owner, limit=1)

    response = await client.get(_STREAM_URL, headers=headers)

    assert response.status_code == 429


async def test_the_stream_yields_a_snapshot_first(
    session: AsyncSession, db_connection: AsyncConnection, make_token: Callable[..., str]
) -> None:
    """The handler is the generator now (T-405), so its first frame is the connect snapshot."""
    owner, _ = await _caller(session, make_token)
    await _document(session, owner_id=owner)

    frames = await _open_stream(session, db_connection, owner_id=owner)
    first = await anext(frames)
    assert first.event == "snapshot"
    await frames.aclose()


async def test_the_slot_dependency_holds_one_and_releases_it_on_close(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-41(7)'s cap, now owned by a `yield` dependency rather than the handler (T-405).

    The release lives in that dependency's `finally`, and FastAPI closes the request-scoped
    exit stack **after** the streaming response completes — so the slot covers exactly the
    stream's lifetime. The previous shape acquired in the handler and released inside the
    publisher, which leaked a slot whenever anything between the two raised.
    """
    owner, _ = await _caller(session, make_token)
    user = await UserRepository(session).get(owner)
    assert user is not None

    slot = hold_stream_slot(user=user, settings=_fast_settings())
    await anext(slot)
    assert document_events.registry.count(owner) == 1

    await slot.aclose()
    assert document_events.registry.count(owner) == 0


async def test_the_slot_dependency_refuses_over_the_cap(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The `429` is raised before any frame, which is the whole reason it is a dependency."""
    owner, _ = await _caller(session, make_token)
    user = await UserRepository(session).get(owner)
    assert user is not None
    document_events.registry.acquire(owner, limit=1)

    slot = hold_stream_slot(user=user, settings=_fast_settings(max_streams_per_user=1))
    with pytest.raises(HTTPException) as excinfo:
        await anext(slot)
    assert excinfo.value.status_code == 429

    document_events.registry.release(owner)
