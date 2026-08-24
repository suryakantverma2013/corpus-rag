"""T-714 document_figures — the pictures a document version carries (FR-ING-09, R-94(5))

T-713 built the detector and left it with no call site; this is where its output becomes
durable. The table is keyed by **document and version**, because R-94(5) puts figures under
R-36's copy -> swap -> collect and R-39's purge-before-commit unchanged — a superseded
version's figures are deleted with its chunks, and a deleted document's with its objects. The
rasters live under the existing version-scoped storage prefix, so the object half of that
parity needs no new code at all: `delete_prefix` on `v{n}/` already removes them.

**Not a chunk.** No embedding, no text into retrieval, no part of `embedding_fingerprint`
(R-94(4)) — which is why FR-ING-09 ships with no `PREPROCESSING_VERSION` bump and forces no
T-608 rebuild. Nothing here is filterable by retrieval, and nothing in `app/rag` may import it.

`content_sha256` is the figure's **public** id (R-94(5)): derived from content, so an unchanged
crop keeps a cached URL across a re-ingestion. It is not the primary key, because one version
may legitimately hold the same crop twice and a key would turn that into a collision.

Revision ID: 402f3492aed5
Revises: a3f21c7be904
Create Date: 2026-08-24 13:16:50.002634
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "402f3492aed5"
down_revision: str | None = "a3f21c7be904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_figures",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("figure_index", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("bbox_x0", sa.Float(), nullable=False),
        sa.Column("bbox_y0", sa.Float(), nullable=False),
        sa.Column("bbox_x1", sa.Float(), nullable=False),
        sa.Column("bbox_y1", sa.Float(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_figures_document_id_documents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_figures")),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            "page_number",
            "figure_index",
            name="uq_document_figures_version_page_index",
        ),
    )
    op.create_index(
        "ix_document_figures_document_version_page",
        "document_figures",
        ["document_id", "document_version", "page_number"],
        unique=False,
    )
    op.create_index(
        "ix_document_figures_document_version_sha",
        "document_figures",
        ["document_id", "document_version", "content_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_figures_document_version_sha", table_name="document_figures")
    op.drop_index("ix_document_figures_document_version_page", table_name="document_figures")
    op.drop_table("document_figures")
