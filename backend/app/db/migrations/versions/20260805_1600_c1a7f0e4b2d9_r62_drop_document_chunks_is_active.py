"""R-62(3) / OI-28 — drop the writerless `document_chunks.is_active`

The flag was introduced for the soft-delete R-36(7) imagined for FR-ING-05. R-39(3) then
ruled FR-ING-05 a **hard** delete — literal to its "removes vectors", and necessary because a
deleted document's chunks are the dominant storage cost (~12 KB each plus an HNSW entry)
while retrieval already excludes the document via `documents.searchable`/`deleted_at`. The
column was consequently never written by anything: rows were created `true`, the version-copy
inserted `literal(True)`, `active_only` was never passed `False` by any caller, and the one
method that could set it false (`ChunkRepository.deactivate`) had a test but no production
caller. So `is_active IS TRUE` ran as a tautology on every retrieval query, and the composite
access index carried a column that could never discriminate.

Dropping it rather than documenting it: a tautological predicate on the hot retrieval path
and a tested method nothing calls are exactly what mislead the next reader, and this is the
cheapest moment to do it — before T-601's scenario tests and before any production data.

The access index is rebuilt without the column and keeps doing its FR-RET-04 / NFR-SEC-06
job; `(tenant_id, knowledge_base_id)` is the part that was ever selective.

**`downgrade` restores the column and the old index, but cannot restore values** — every row
returns as `true`, which is what every row already held.

Revision ID: c1a7f0e4b2d9
Revises: 0ff884b4781a
Create Date: 2026-08-05 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a7f0e4b2d9"
down_revision: str | None = "0ff884b4781a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_INDEX = "ix_document_chunks_tenant_id_knowledge_base_id_is_active"
_NEW_INDEX = "ix_document_chunks_tenant_id_knowledge_base_id"


def upgrade() -> None:
    op.drop_index(_OLD_INDEX, table_name="document_chunks")
    op.create_index(_NEW_INDEX, "document_chunks", ["tenant_id", "knowledge_base_id"])
    op.drop_column("document_chunks", "is_active")


def downgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.drop_index(_NEW_INDEX, table_name="document_chunks")
    op.create_index(
        _OLD_INDEX,
        "document_chunks",
        ["tenant_id", "knowledge_base_id", "is_active"],
    )
