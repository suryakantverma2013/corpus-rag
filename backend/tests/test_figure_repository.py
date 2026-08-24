"""`document_figures` writes, and the boundary that keeps a figure out of retrieval (T-714).

Two things are asserted here that the end-to-end scenarios cannot state as sharply:

* `replace_for_version` is **idempotent and collecting** — running it twice leaves one version's
  rows, and running it for v2 leaves none of v1's. The scenarios prove this happens through the
  worker; this proves it is a property of the call rather than of the path that made it.
* **R-94(4) is structural.** A figure takes no embedding and is not retrievable, which is only
  true while nothing in `app/rag` can reach the table. That is a statement about imports, and
  imports are the one thing a behavioural test cannot see.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.models.document_figure import DocumentFigure
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository


def _figure(
    document_id: uuid.UUID, *, version: int, page: int = 1, index: int = 0
) -> DocumentFigure:
    digest = f"{page:02d}{index:02d}{version:02d}".ljust(64, "0")
    return DocumentFigure(
        document_id=document_id,
        document_version=version,
        page_number=page,
        figure_index=index,
        content_sha256=digest,
        storage_uri=f"file:///figures/{digest}.png",
        caption="FIGURE 1",
        bbox_x0=10.0,
        bbox_y0=20.0,
        bbox_x1=110.0,
        bbox_y1=140.0,
        width_px=200,
        height_px=240,
        byte_size=1234,
    )


async def _rows(session: AsyncSession, document_id: uuid.UUID) -> list[DocumentFigure]:
    stmt = select(DocumentFigure).where(DocumentFigure.document_id == document_id)
    return list((await session.scalars(stmt)).all())


@pytest.fixture
async def document_id(session: AsyncSession) -> uuid.UUID:
    """A real `documents` row, because `document_figures` holds a foreign key to one."""
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@corpus.test"
    )
    kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
    document = await DocumentRepository(session).add(
        Document(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="handbook.pdf",
            storage_uri="s3://corpus/handbook.pdf",
            checksum_sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.ACTIVE,
            searchable=True,
        )
    )
    return document.id


async def test_replace_writes_the_version_rows(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    repo = DocumentFigureRepository(session)

    written = await repo.replace_for_version(
        document_id,
        document_version=1,
        figures=[
            _figure(document_id, version=1, index=0),
            _figure(document_id, version=1, index=1),
        ],
    )

    assert written == 2
    assert len(await _rows(session, document_id)) == 2


async def test_replacing_the_same_version_twice_does_not_double_the_rows(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    """The redelivery case, and the reason the delete is unconditional.

    The unique constraint would otherwise turn an FR-ING-04 retry into an integrity error — which
    the worker's fail-open handler would swallow, leaving a document that reports no figures for
    a reason nobody can see.
    """
    repo = DocumentFigureRepository(session)
    for _ in range(2):
        await repo.replace_for_version(
            document_id, document_version=1, figures=[_figure(document_id, version=1)]
        )

    assert len(await _rows(session, document_id)) == 1


async def test_a_new_version_collects_the_previous_one(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    """R-94(5) via R-36's collect. A surviving v1 row points at bytes the purge has deleted."""
    repo = DocumentFigureRepository(session)
    await repo.replace_for_version(
        document_id, document_version=1, figures=[_figure(document_id, version=1)]
    )

    await repo.replace_for_version(
        document_id, document_version=2, figures=[_figure(document_id, version=2)]
    )

    assert [row.document_version for row in await _rows(session, document_id)] == [2]


async def test_replacing_with_nothing_collects_and_leaves_the_table_empty(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    """An empty list is not a no-op — it says *this document has no figures*.

    Which is exactly true when extraction is switched off between two ingestions, and is what
    stops that operator action creating rows nothing will ever collect.
    """
    repo = DocumentFigureRepository(session)
    await repo.replace_for_version(
        document_id, document_version=1, figures=[_figure(document_id, version=1)]
    )

    assert await repo.replace_for_version(document_id, document_version=2, figures=[]) == 0
    assert await _rows(session, document_id) == []


async def test_delete_by_document_removes_every_version(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    repo = DocumentFigureRepository(session)
    session.add_all([_figure(document_id, version=1), _figure(document_id, version=2)])
    await session.flush()

    assert await repo.delete_by_document(document_id) == 2
    assert await _rows(session, document_id) == []


# --- R-94(4): a figure is presentation data, and structurally so --------------


def test_nothing_in_the_rag_package_reaches_the_figure_table() -> None:
    """A figure takes no embedding, carries no text into retrieval and is not a chunk.

    Behaviourally that is a claim about every query in `app/rag`, which no single test can
    check. As an import rule it is one line — and it is the rule that keeps FR-ING-09 free to
    change its detector without invalidating a vector (R-94(4)), because a subsystem that
    cannot see the table cannot come to depend on it.

    `app/api` is deliberately **not** in scope: T-715 puts the figure route there.
    """
    rag_dir = Path(__file__).resolve().parents[1] / "app" / "rag"
    offenders = [
        path.name
        for path in rag_dir.rglob("*.py")
        if "document_figure" in path.read_text(encoding="utf-8")
        or "repositories.figures" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        f"{offenders} reach the figure table from the retrieval and generation package — "
        "R-94(4) makes a figure presentation data that feeds no embedding and no retrieval"
    )
