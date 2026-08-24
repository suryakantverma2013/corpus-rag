"""FR-CIT-07 — the figure under the citation (T-716, R-94(3)).

Three things are asserted here that no other module can state:

* **the lookup is selected by the locator, and refuses everything NFR-SEC-10 refuses.** The
  repository shares its predicates with T-715's route through `_servable_predicates`, so each
  one is driven against the state only it catches — the same discipline `test_figure_route.py`
  applies to the route, for the same reason: `searchable` and `status == ACTIVE` overlap on
  almost every row, and testing them against one document would leave either free to be
  deleted.
* **all four surfaces agree.** The transcript, the FR-MSG-08 feedback response and the `message`
  frame on send *and* regenerate all reach `segs` through three call sites, and a figure that
  appeared on one and not another is a defect nobody sees until a user rates an answer and
  watches its pictures vanish.
* **a transcript costs one query.** "Batched" is an intention until something counts the
  statements, and resolving inside the per-message loop is the shape a later refactor drifts
  back into.

Nothing here touches object storage: `list_for_citations` resolves *metadata*, and the bytes are
T-715's route. That is why a figure can be seeded without a raster.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.document_figure import DocumentFigure
from app.db.models.message import Message
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY

pytestmark = pytest.mark.usefixtures("patch_jwks")

_PAGE = 7
_ANSWER = "The theorem guarantees a root exists."


# ---- fixtures ----------------------------------------------------------------------


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.test"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _cited_page(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    searchable: bool = True,
    deleted: bool = False,
    current_version: int = 1,
    chunk_version: int = 1,
    figure_version: int = 1,
    figures: int = 1,
    page: int = _PAGE,
) -> tuple[Document, DocumentChunk, list[DocumentFigure]]:
    """A document, one chunk citing `page`, and that page's figures.

    The three versions are separate parameters on purpose: the interesting refusals are the ones
    where they disagree, which is what a `/replace` produces.
    """
    from datetime import UTC, datetime

    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        mime_type="application/pdf",
        storage_uri=f"file:///objects/{uuid.uuid4()}/original.pdf",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=2048,
        status=status,
        searchable=searchable,
        current_version=current_version,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(document)
    await session.flush()

    chunk = DocumentChunk(
        document_id=document.id,
        document_version=chunk_version,
        chunk_index=0,
        chunk_hash=uuid.uuid4().hex * 2,
        embedding_fingerprint=uuid.uuid4().hex * 2,
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=kb.id,
        chunk_text="the passage",
        meta={"locator": {"kind": "page", "label": f"p. {page}", "page": page}},
    )
    session.add(chunk)

    rows = [
        DocumentFigure(
            document_id=document.id,
            document_version=figure_version,
            page_number=page,
            figure_index=index,
            content_sha256=f"{index:02d}".ljust(64, "a"),
            storage_uri=f"file:///objects/figures/{index}.png",
            caption="FIGURE 3" if index == 0 else "",
            bbox_x0=10.0,
            bbox_y0=20.0,
            bbox_x1=110.0,
            bbox_y1=140.0,
            width_px=320,
            height_px=240,
            byte_size=1024,
        )
        for index in range(figures)
    ]
    session.add_all(rows)
    await session.flush()
    return document, chunk, rows


async def _another_page(
    session: AsyncSession, *, document: Document, page: int, chunk_index: int
) -> tuple[DocumentChunk, list[DocumentFigure]]:
    """A second cited page on an existing document, with its own figure."""
    chunk = DocumentChunk(
        document_id=document.id,
        document_version=document.current_version,
        chunk_index=chunk_index,
        chunk_hash=uuid.uuid4().hex * 2,
        embedding_fingerprint=uuid.uuid4().hex * 2,
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=document.knowledge_base_id,
        chunk_text="another passage",
        meta={"locator": {"kind": "page", "label": f"p. {page}", "page": page}},
    )
    figure = DocumentFigure(
        document_id=document.id,
        document_version=document.current_version,
        page_number=page,
        figure_index=0,
        content_sha256=f"{page:02d}".ljust(64, "b"),
        storage_uri=f"file:///objects/figures/p{page}.png",
        caption="",
        bbox_x0=10.0,
        bbox_y0=20.0,
        bbox_x1=110.0,
        bbox_y1=140.0,
        width_px=320,
        height_px=240,
        byte_size=1024,
    )
    session.add_all([chunk, figure])
    await session.flush()
    return chunk, [figure]


def _envelope(chunk: DocumentChunk, *, page: int = _PAGE, kind: str = "page") -> dict:
    """The `messages.citations` payload a real answer carries, citing `chunk`."""
    locator: dict[str, object] = {"kind": kind, "label": f"p. {page}"}
    if kind == "page":
        locator["page"] = page
    else:
        locator["section_index"] = page
    return {
        SEGMENTS_KEY: [
            {"text": _ANSWER},
            {
                "isCite": True,
                "doc": "handbook.pdf",
                "page": f"p. {page}",
                "locator": locator,
                "quote": "the passage",
                "chunkId": str(chunk.id),
            },
        ],
        SOURCE_IDS_KEY: [str(chunk.id)],
    }


async def _answer(
    session: AsyncSession, *, owner_id: uuid.UUID, chunk: DocumentChunk, **envelope_kwargs
) -> tuple[Conversation, Message]:
    conversation = Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role=MessageRole.USER, content="Explain it.")
    )
    message = Message(
        conversation_id=conversation.id,
        role=MessageRole.AI,
        content=_ANSWER,
        citations=_envelope(chunk, **envelope_kwargs),
    )
    session.add(message)
    await session.flush()
    return conversation, message


# ---- the lookup: selected by locator, refused by NFR-SEC-10 -------------------------


async def test_a_cited_page_resolves_the_figures_printed_on_it(session: AsyncSession) -> None:
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, rows = await _cited_page(session, owner_id=owner, figures=2)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert [figure.content_sha256 for figure in found[chunk.id]] == [
        row.content_sha256 for row in rows
    ]


async def test_the_figures_come_back_in_the_documents_own_order(session: AsyncSession) -> None:
    """`figure_index`, not insertion order — R-94(2)'s (a)/(b) panels read left to right."""
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(session, owner_id=owner, figures=3)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert [figure.figure_index for figure in found[chunk.id]] == [0, 1, 2]


async def test_another_page_of_the_same_document_resolves_nothing(session: AsyncSession) -> None:
    """The locator selects the page, so a citation to page 8 must not pick up page 7's figure."""
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(session, owner_id=owner)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE + 1}, owner_id=owner
    )

    assert found == {}


async def test_two_cited_pages_of_one_document_do_not_share_their_figures(
    session: AsyncSession,
) -> None:
    """The pair `(chunk, page)` is the key, not two independent `IN` sets.

    **Two pages of the SAME document**, and that is the whole test. Across two documents the
    join on `document_id` already separates them, so a cross-document version passes against
    the broken key and proves nothing — which is what the first draft of this test did. Here,
    `chunk_id IN (a, b) AND page_number IN (4, 7)` hands each citation both figures.
    """
    owner, _ = await _caller(session, lambda **_: "")
    document, chunk_a, rows_a = await _cited_page(session, owner_id=owner, page=4)
    chunk_b, rows_b = await _another_page(session, document=document, page=7, chunk_index=1)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk_a.id: 4, chunk_b.id: 7}, owner_id=owner
    )

    assert [f.content_sha256 for f in found[chunk_a.id]] == [rows_a[0].content_sha256]
    assert [f.content_sha256 for f in found[chunk_b.id]] == [rows_b[0].content_sha256]


async def test_no_citations_costs_no_query(session: AsyncSession) -> None:
    """An empty ask returns without a statement — the non-PDF corpus's whole cost."""
    owner, _ = await _caller(session, lambda **_: "")
    with _count_statements(session) as counter:
        found = await DocumentFigureRepository(session).list_for_citations({}, owner_id=owner)
    assert found == {}
    assert counter.total == 0


async def test_another_users_figure_is_never_resolved(session: AsyncSession) -> None:
    """NFR-SEC-10's isolation boundary, and the one with no administrator branch."""
    owner, _ = await _caller(session, lambda **_: "")
    stranger, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(session, owner_id=owner)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=stranger
    )

    assert found == {}


async def test_a_soft_deleted_documents_figure_is_not_resolved(session: AsyncSession) -> None:
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(session, owner_id=owner, deleted=True)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert found == {}


async def test_a_non_searchable_documents_figure_is_not_resolved(session: AsyncSession) -> None:
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(session, owner_id=owner, searchable=False)

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert found == {}


async def test_a_document_mid_replace_resolves_no_figure(session: AsyncSession) -> None:
    """The one state that tells `searchable` and `status` apart (R-40(3)).

    A document being replaced keeps answering — it stays `searchable` and leaves `ACTIVE` — so
    without the status predicate this row would serve. FR-CIT-07 sanctions showing no figure for
    the duration; what it does not sanction is showing one the route would then refuse.
    """
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(
        session, owner_id=owner, status=DocumentStatus.PARSING, searchable=True
    )

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert found == {}


async def test_a_replaced_documents_older_figures_are_not_resolved(session: AsyncSession) -> None:
    """A figure row that outlived its version is unreachable rather than a 404 to explain."""
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(
        session, owner_id=owner, current_version=2, chunk_version=2, figure_version=1
    )

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert found == {}


async def test_a_chunk_from_a_superseded_version_resolves_nothing(session: AsyncSession) -> None:
    """R-36 does this one for free, and the freeness is what makes it worth asserting.

    A `/replace` deletes the superseded version's chunk rows, so a historical citation joins to
    nothing. Here the chunk is deliberately left behind at v1 while the document has moved to
    v2, which is the state the join must refuse even if a row survived: page 7 of the new
    version is not the page that answer cited.
    """
    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, _ = await _cited_page(
        session, owner_id=owner, current_version=2, chunk_version=1, figure_version=2
    )

    found = await DocumentFigureRepository(session).list_for_citations(
        {chunk.id: _PAGE}, owner_id=owner
    )

    assert found == {}


# ---- the four surfaces -------------------------------------------------------------


async def test_the_transcript_carries_the_figure(client, session: AsyncSession, make_token) -> None:  # noqa: ANN001
    owner, headers = await _caller(session, make_token)
    document, chunk, rows = await _cited_page(session, owner_id=owner)
    conversation, _ = await _answer(session, owner_id=owner, chunk=chunk)

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    assert response.status_code == 200
    citation = next(seg for seg in response.json()[1]["segs"] if seg.get("isCite"))
    assert citation["figures"] == [
        {
            "documentId": str(document.id),
            "contentSha256": rows[0].content_sha256,
            "caption": "FIGURE 3",
            "widthPx": 320,
            "heightPx": 240,
        }
    ]


async def test_the_feedback_response_carries_the_same_figure(
    client, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """FR-MSG-08 returns the whole message, and the client replaces the one on screen with it.

    Resolve on the transcript and not here and every figure in an answer disappears the moment
    the user clicks a thumb — a defect with no error, no log line and no failing test anywhere
    else in the suite.
    """
    owner, headers = await _caller(session, make_token)
    _, chunk, rows = await _cited_page(session, owner_id=owner)
    _, message = await _answer(session, owner_id=owner, chunk=chunk)

    response = await client.post(
        f"/api/v1/messages/{message.id}/feedback", json={"feedback": "up"}, headers=headers
    )

    assert response.status_code == 200
    citation = next(seg for seg in response.json()["segs"] if seg.get("isCite"))
    assert [figure["contentSha256"] for figure in citation["figures"]] == [rows[0].content_sha256]


async def test_a_citation_with_no_figure_carries_no_key(
    client, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """FR-CIT-07: "a citation with no figure renders exactly as it does today"."""
    owner, headers = await _caller(session, make_token)
    _, chunk, _ = await _cited_page(session, owner_id=owner, figures=0)
    conversation, _ = await _answer(session, owner_id=owner, chunk=chunk)

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    citation = next(seg for seg in response.json()[1]["segs"] if seg.get("isCite"))
    assert "figures" not in citation


async def test_a_locator_with_no_page_resolves_no_figure(
    client, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """R-34: DOCX, Markdown and CSV have no pages, so their citations have nothing to select by.

    Note what this does *not* prove — it passes whether the `kind` check is present or not,
    because a section locator carries no `page` either way. The test below is the one that
    separates them.
    """
    owner, headers = await _caller(session, make_token)
    _, chunk, _ = await _cited_page(session, owner_id=owner)
    conversation, _ = await _answer(session, owner_id=owner, chunk=chunk, kind="section")

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    citation = next(seg for seg in response.json()[1]["segs"] if seg.get("isCite"))
    assert "figures" not in citation


async def test_a_non_page_locator_carrying_a_page_is_still_refused(
    client, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """The `kind` check, driven against the only state that reaches it.

    R-34 never writes a `page` onto a `section` locator, so this shape comes from a hand-built
    or older envelope — which is exactly the input a tolerant read-back has to survive. The
    hazard is specific: a section ordinal is a number in a different space, so accepting it
    joins happily and renders an unrelated page's picture under someone's answer.
    """
    owner, headers = await _caller(session, make_token)
    document, chunk, _ = await _cited_page(session, owner_id=owner)
    conversation, message = await _answer(session, owner_id=owner, chunk=chunk)
    envelope = dict(message.citations)
    segments = [dict(segment) for segment in envelope[SEGMENTS_KEY]]
    segments[1]["locator"] = {"kind": "section", "label": "§ Setup", "page": _PAGE}
    message.citations = {**envelope, SEGMENTS_KEY: segments}
    await session.flush()

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    citation = next(seg for seg in response.json()[1]["segs"] if seg.get("isCite"))
    assert "figures" not in citation, (
        f"a section locator claiming page {_PAGE} of {document.filename} resolved a figure"
    )


async def test_a_transcript_resolves_its_figures_in_one_query(
    client, session: AsyncSession, make_token
) -> None:  # noqa: ANN001
    """One query for the whole transcript, not one per message.

    Ten answers, each citing its own document's page. Resolving inside the per-message loop is
    correct and ten times as expensive, so only a statement counter can tell the two apart.
    """
    owner, headers = await _caller(session, make_token)
    conversation = Conversation(owner_id=owner, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    for _ in range(10):
        _, chunk, _ = await _cited_page(session, owner_id=owner)
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.AI,
                content=_ANSWER,
                citations=_envelope(chunk),
            )
        )
    await session.flush()

    with _count_statements(session) as counter:
        response = await client.get(
            f"/api/v1/conversations/{conversation.id}/messages", headers=headers
        )

    assert response.status_code == 200
    assert len(response.json()) == 10
    assert counter.figure_queries == 1, (
        f"{counter.figure_queries} queries against document_figures for one transcript — "
        "the lookup moved inside the per-message loop"
    )


async def test_the_message_frame_carries_the_figure(session: AsyncSession, db_connection) -> None:  # noqa: ANN001
    """The `message` frame is the *fourth* surface, and it is served by send and regenerate.

    Driven at the frame builder rather than through a whole turn, because the offline chat
    suite's turns abstain — there is no corpus, so there is no citation to hang a figure on, and
    a test that drove one would be asserting the fake retriever rather than this. What it does
    exercise is the seam that makes this surface different from the other three: neither
    streaming handler carries a `DbSession` (T-210), so the builder is `async` and opens its own
    short session from the handler's sessionmaker. Revert it to a sync function and this fails —
    which is the whole point, because the alternative failure is silent: an answer whose
    pictures appear only after a reload.
    """
    from app.api.messages import _message_data
    from app.services.chat import MessageEvent

    owner, _ = await _caller(session, lambda **_: "")
    _, chunk, rows = await _cited_page(session, owner_id=owner)
    _, message = await _answer(session, owner_id=owner, chunk=chunk)

    def _sessionmaker() -> AsyncSession:
        return AsyncSession(
            bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    data = await _message_data(
        MessageEvent(message=message, text=_ANSWER, outcome="answered", error_code=None),
        sessionmaker=_sessionmaker,
        owner_id=owner,
    )

    published = data.model_dump(mode="json")["message"]["segs"]
    citation = next(seg for seg in published if seg.get("isCite"))
    assert [figure["contentSha256"] for figure in citation["figures"]] == [rows[0].content_sha256]


async def test_a_degraded_frame_opens_no_session(session: AsyncSession) -> None:
    """An FR-ORC-05 failure has no row and no citation, so it must not reach the database.

    `DegradedMessage.segs` is `list[TextSegment]` — there is nothing to resolve. The
    sessionmaker here raises, so a builder that opened one regardless fails loudly instead of
    costing every errored turn a pointless query.
    """
    from app.api.messages import _message_data
    from app.services.chat import MessageEvent

    def _sessionmaker() -> AsyncSession:
        raise AssertionError("a degraded frame must not open a session")

    data = await _message_data(
        MessageEvent(message=None, text="Something went wrong.", outcome="error", error_code="X"),
        sessionmaker=_sessionmaker,
        owner_id=uuid.uuid4(),
        fallback_id=None,
    )

    assert data.model_dump(mode="json")["message"]["segs"] == [{"text": "Something went wrong."}]


# ---- counting statements -----------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    @property
    def total(self) -> int:
        return len(self.statements)

    @property
    def figure_queries(self) -> int:
        return sum("document_figures" in statement for statement in self.statements)


class _count_statements:  # noqa: N801 — a context manager used as one
    """Record every SQL statement issued on `session`'s connection.

    Bound to the synchronous engine behind the async session, because that is where SQLAlchemy
    emits `before_cursor_execute`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._engine = session.get_bind().engine
        self.counter = _Counter()

    def __enter__(self) -> _Counter:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self.counter

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(self, _conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        self.counter.statements.append(statement)
