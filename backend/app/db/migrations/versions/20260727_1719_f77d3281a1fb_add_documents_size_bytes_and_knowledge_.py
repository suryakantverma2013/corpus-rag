"""add documents.size_bytes and knowledge_bases.conversation_id

Both columns are what T-202's upload API needs and the T-101 schema lacked (R-33):

* ``documents.size_bytes`` — nothing tracked bytes, so the FR-ERR-02 10 GB/user quota
  was unenforceable. Summed per owner over non-deleted rows.
* ``knowledge_bases.conversation_id`` — R-25 maps THIS-CHAT attachments to an implicit
  per-conversation KB, but no column linked the two in either direction. Unique, so a
  concurrent double-upload to one conversation cannot create two attachment KBs.

Both are nullable and additive: existing rows stay valid and the downgrade is a clean
drop.

Revision ID: f77d3281a1fb
Revises: dd66302d4794
Create Date: 2026-07-27 17:19:05.146520
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f77d3281a1fb"
down_revision: str | None = "dd66302d4794"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("knowledge_bases", sa.Column("conversation_id", sa.UUID(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_knowledge_bases_conversation_id"), "knowledge_bases", ["conversation_id"]
    )
    op.create_foreign_key(
        op.f("fk_knowledge_bases_conversation_id_conversations"),
        "knowledge_bases",
        "conversations",
        ["conversation_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_knowledge_bases_conversation_id_conversations"),
        "knowledge_bases",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_knowledge_bases_conversation_id"), "knowledge_bases", type_="unique"
    )
    op.drop_column("knowledge_bases", "conversation_id")
    op.drop_column("documents", "size_bytes")
