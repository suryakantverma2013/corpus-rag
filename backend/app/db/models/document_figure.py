"""`document_figures` — the pictures a document version carries (FR-ING-09, T-714, R-94(5)).

**Keyed by document *version*, because that is what a figure is a fact about.** R-94(5) puts
this table under R-36's copy → swap → collect and R-39's purge-before-commit unchanged: a
superseded version's figures are deleted with its chunks, and a deleted document's figures go
with its objects. The rasters live under the existing version-scoped storage prefix, which is
what makes the object half of that parity free — `delete_prefix` on `v{n}/` already removes
them, and always did.

**Not a chunk, and structurally not one.** A figure takes no embedding, carries no text into
retrieval and is no part of `embedding_fingerprint` (R-94(4)) — which is why FR-ING-09 shipped
with no `PREPROCESSING_VERSION` bump and forces no T-608 rebuild. There is deliberately no
column here that retrieval could filter on and no route from `app.rag` to this model; a test
asserts the second, because the first is only a habit.

**`content_sha256` is the public id; `id` is not.** R-94(5) derives a figure's id from its
content so a re-ingestion producing the same crop keeps a URL a browser cached — which is what
lets T-715's route set a long immutable cache lifetime. It is not the primary key because the
same crop can legitimately appear twice in one version (a repeated diagram, a two-panel figure
whose halves render identically), and a key would turn correct behaviour into a collision.

**No `tenant_id` / `knowledge_base_id`.** `document_chunks` carries both because the retrieval
query filters on them directly; nothing filters figures. T-715's route resolves the *document*
under the same predicate as FR-RET-04 and then reads its figures, so a copy of the scope here
would be a second answer to a question this table never asks — free to drift, and authoritative
for nothing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CHAR, Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class DocumentFigure(CreatedAtMixin, Base):
    __tablename__ = "document_figures"
    __table_args__ = (
        # The detector's own output identity: one region, at one ordinal, on one page, of one
        # version. It is what makes the write idempotent — a redelivered job re-inserts the
        # same rows rather than doubling them.
        UniqueConstraint(
            "document_id",
            "document_version",
            "page_number",
            "figure_index",
            name="uq_document_figures_version_page_index",
        ),
        # FR-CIT-07's only query: the figures of the *current* version of a cited document, on
        # the page a citation's locator names.
        Index(
            "ix_document_figures_document_version_page",
            "document_id",
            "document_version",
            "page_number",
        ),
        # T-715's only query: one figure by its public, content-derived id.
        Index(
            "ix_document_figures_document_version_sha",
            "document_id",
            "document_version",
            "content_sha256",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 1-based, matching `Locator.page`. FR-CIT-07 joins on exactly this.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 0-based ordinal within the page, in the detector's reading order.
    figure_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    #: The document's own caption, or ``""`` where it declares none — never NULL and never
    #: synthesised (R-34's rule about inventing what a format does not state). The empty string
    #: is a fact about the page; NULL would be a third state nothing here means.
    caption: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: The region in PDF points, as the detector found it.
    bbox_x0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y0: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_x1: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y1: Mapped[float] = mapped_column(Float, nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
