"""T-728: documents.text_quality_ratio (FR-ING-10, R-100 §8.90).

One nullable column travelling the path `page_count` already travels. Nullable and with no
server default on purpose: `NULL` means **not measured** — every document ingested before
this migration, every non-PDF, and a PDF that extracted no characters at all — which is a
different fact from `0.0`, "measured and clean". Backfilling would assert a measurement that
was never taken, on exactly the corpus most likely to contain the documents this exists to
find.

No `PREPROCESSING_VERSION` bump and no re-embedding: the value is presentation metadata and
takes no part in `embedding_fingerprint` (R-100(9)). An existing corpus reports `NULL` until
a document is next re-ingested for some other reason, which is correct rather than a gap.

Revision ID: c4d81b3e7a29
Revises: b91d4c7a2f10
Create Date: 2026-08-29 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d81b3e7a29"
down_revision = "b91d4c7a2f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("text_quality_ratio", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "text_quality_ratio")
