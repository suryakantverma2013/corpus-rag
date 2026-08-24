"""`document_figures` queries (FR-ING-09, T-714, R-94(5)).

**Two reads and two writes.** Both reads are NFR-SEC-10's access decision, each in one query,
and they share their predicates through :func:`_servable_predicates` rather than restating
them — two places that both decide who may see a figure is how one of them comes to be wrong.
The writes are what lifetime parity needs.

The reads differ only in what selects the figure: :meth:`get_servable` takes a content hash
(T-715's route, "serve me *this* figure") and :meth:`list_for_citations` takes a citation's
locator (T-716's FR-CIT-07 lookup, "what is printed on the page this answer cites"). Because
the predicates are shared, the transcript can never advertise a figure the route would refuse,
nor hide one it would serve.

The shape mirrors `DocumentChunkRepository` deliberately, because the rules are the same ones:
never commit (the caller owns the unit of work), collect superseded versions in the same
statement sequence as the insert, and hard-delete on FR-ING-05.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from sqlalchemy import ColumnElement, delete, select, tuple_

from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.document_figure import DocumentFigure
from app.db.repositories.base import BaseRepository


def _servable_predicates(owner_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """NFR-SEC-10's access decision, in one place because it is taken in two.

    Every clause is explained at :meth:`DocumentFigureRepository.get_servable`, which is where
    the reasoning belongs; what matters here is that there is exactly one copy of it. A caller
    must still join :class:`Document` itself — these are predicates, not a join.
    """
    return (
        DocumentFigure.document_version == Document.current_version,
        Document.owner_id == owner_id,
        Document.deleted_at.is_(None),
        Document.searchable.is_(True),
        Document.status == DocumentStatus.ACTIVE,
    )


class DocumentFigureRepository(BaseRepository[DocumentFigure]):
    model = DocumentFigure

    async def get_servable(
        self,
        document_id: uuid.UUID,
        *,
        content_sha256: str,
        owner_id: uuid.UUID,
    ) -> DocumentFigure | None:
        """One figure a caller may be shown, or ``None`` for every reason (NFR-SEC-10).

        **One query, five predicates, and `None` for all of them**, because the route renders
        every miss as the same 404 (NFR-SEC-02): a wrong id, someone else's document, a
        deleted one, one mid-replace and one whose version has moved on are not
        distinguishable from outside, and separating them here would invite a route that
        separates them too.

        The predicate set is FR-RET-04's, as NFR-SEC-10 requires, plus the two clauses that
        requirement adds:

        * ``owner_id`` — **the isolation boundary, and there is no administrator branch.**
          Every sibling route on `/documents` widens for an administrator under FR-USR-04;
          this one must not, and the difference is not an oversight to be harmonised away.
          What those routes widen is *management* — list, read metadata, delete, retry — and
          none of them discloses a document's **content**. This serves a rendered region of a
          page. Widening it would create the first route in Corpus by which an administrator
          reads another user's document contents, which is a privacy change no requirement
          asks for and NFR-SEC-10's "the same predicate as FR-RET-04" (which has no
          administrator branch) refuses in as many words.
        * ``deleted_at IS NULL`` and ``searchable IS TRUE`` — FR-RET-04's own two.
        * ``status == ACTIVE`` — NFR-SEC-10's, and **not** a duplicate of ``searchable``.
          `_access_predicates` deliberately omits status so a document keeps answering while
          its replacement is built (R-40(3)), which is exactly the state where the two differ:
          mid-replace a document is ``searchable`` and not ``ACTIVE``. Both are tested against
          the state only they catch.
        * ``document_version == current_version`` — the superseded rows are collected in the
          swap, so this is defence in depth, and it is what makes a row that outlived its
          version unreachable rather than a 404 someone has to explain.

        There is deliberately **no tenant predicate**: `Document.tenant_id` is a property of
        the row rather than of the caller under OI-21's single-org disposition, exactly as
        `DocumentRepository.get_listing_scoped` has it. When multi-tenancy lands the predicate
        comes from the principal, in one place, for every route at once.
        """
        stmt = (
            select(DocumentFigure)
            .join(Document, Document.id == DocumentFigure.document_id)
            .where(
                DocumentFigure.document_id == document_id,
                DocumentFigure.content_sha256 == content_sha256,
                *_servable_predicates(owner_id),
            )
        )
        return (await self.session.scalars(stmt)).first()

    async def list_for_citations(
        self,
        pages_by_chunk_id: Mapping[uuid.UUID, int],
        *,
        owner_id: uuid.UUID,
    ) -> dict[uuid.UUID, list[DocumentFigure]]:
        """The figures printed on the pages a turn's citations name (FR-CIT-07, R-94(3)).

        Keyed by cited chunk id, so the caller never re-derives which citation a figure belongs
        to. A chunk with no figures on its page is **absent** from the result rather than
        present with an empty list — "this citation resolved nothing" and "this citation was
        not asked about" are the same fact to every caller, and one of them is cheaper.

        **The figure is chosen by the locator and never by the model** (R-94(3)): the caller
        supplies `{chunk_id: page_number}` read from the citation's own `Locator`, so nothing
        the LLM wrote can select, name or describe what is shown.

        **Resolved when the citation is served, not when it was composed**, which is the whole
        reason this is a query rather than a column on `messages.citations`. Every predicate
        below can change after the turn — a document can be deleted, replaced, or go
        non-`searchable` — so a *stored* reference would assert "there is a figure here" under
        a decision taken at write time, and the client would then request bytes it may no
        longer be entitled to and render a hole. Resolving here makes the presence of the
        reference and the servability of the bytes **one decision, at one instant, under one
        predicate**. (It also keeps `app/rag` clear of this table, which R-94(4) requires and
        `tests/test_figure_repository.py` enforces as an import rule.)

        **The join to `document_chunks` is doing more work than it looks.** It supplies the
        document *and the version* the citation was resolved against, and R-36 deletes a
        superseded version's chunk rows in the swap — so a citation whose document has since
        been replaced joins to nothing and renders with its denormalised `quote` and no
        figure. That is correct rather than merely convenient: after a replace the page
        numbering may have moved, and page 157's new figure is not the one that answer cited.

        Keyed on the `(chunk_id, page_number)` pair rather than two `IN` sets, so a citation to
        page 4 of one document cannot pick up page 4 of another that happens to be cited in the
        same turn, and a 300-page manual contributes no over-fetch.
        """
        if not pages_by_chunk_id:
            return {}

        stmt = (
            select(DocumentChunk.id, DocumentFigure)
            .join(
                DocumentChunk,
                (DocumentChunk.document_id == DocumentFigure.document_id)
                & (DocumentChunk.document_version == DocumentFigure.document_version),
            )
            .join(Document, Document.id == DocumentFigure.document_id)
            .where(
                tuple_(DocumentChunk.id, DocumentFigure.page_number).in_(
                    list(pages_by_chunk_id.items())
                ),
                *_servable_predicates(owner_id),
            )
            .order_by(DocumentFigure.page_number, DocumentFigure.figure_index)
        )

        found: dict[uuid.UUID, list[DocumentFigure]] = {}
        for chunk_id, figure in await self.session.execute(stmt):
            found.setdefault(chunk_id, []).append(figure)
        return found

    async def replace_for_version(
        self,
        document_id: uuid.UUID,
        *,
        document_version: int,
        figures: Iterable[DocumentFigure],
    ) -> int:
        """Leave this document holding exactly ``figures``, all at ``document_version``.

        One call rather than three, for `persist_chunk_set`'s reason: a caller that has to
        remember the collect step is a caller that will eventually leave two versions' figures
        in the table, and the second set is only discoverable by someone wondering why a
        deleted version still has rows.

        **Delete-then-insert, unconditionally.** The delete of this version's own rows is what
        makes the write idempotent under a redelivered job — the unique constraint would
        otherwise turn a retry into an integrity error, and a retry is the normal case here,
        not the exception.

        Does not commit.
        """
        await self.session.execute(
            delete(DocumentFigure).where(DocumentFigure.document_id == document_id)
        )
        rows = list(figures)
        if rows:
            self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def delete_by_document(self, document_id: uuid.UUID) -> int:
        """Every version's rows, hard-deleted — FR-ING-05, on `DocumentChunkRepository`'s
        reasoning.

        The objects go with the document's storage prefix, which the delete worker already
        purges; this is the half of R-39(3)'s parity that the prefix cannot reach.
        """
        result = await self.session.execute(
            delete(DocumentFigure).where(DocumentFigure.document_id == document_id)
        )
        await self.session.flush()
        return result.rowcount
