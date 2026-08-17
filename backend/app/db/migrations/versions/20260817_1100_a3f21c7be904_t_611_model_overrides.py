"""T-611 model_overrides — operator-selectable models at runtime (R-83)

The six model ids were already one per job, and every one of them was frozen three ways:
`get_settings()` is `@lru_cache`d, the chat and embedding clients are process-wide
singletons, and `OpenAIChatClient` copies the ids into instance attributes at construction.
Moving the answer model therefore meant restarting the API *and* the worker — two separate
process families, which is also why this is a table rather than a reload endpoint or a
mutable global: neither of those crosses a process boundary.

**Empty is the normal state.** Environment remains the default and a row is an override, so
a deployment that never writes here behaves exactly as it did before this table existed.
Clearing an override is a `DELETE`, which is what makes "revert to what the deployment
shipped with" expressible at all.

No index beyond the primary key: the table holds at most one row per slot — five today, six
once T-608 unblocks embeddings — and its only read is an unfiltered `SELECT` of the lot.
`slot` is a plain `VARCHAR` rather than an enum column on `turn_telemetry.outcome`'s
precedent: a CHECK constraint would make adding that sixth slot a migration on a table
nothing else depends on.

Revision ID: a3f21c7be904
Revises: 73a7dfdf7582
Create Date: 2026-08-17 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f21c7be904"
down_revision: str | None = "73a7dfdf7582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_overrides",
        sa.Column("slot", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("slot"),
    )


def downgrade() -> None:
    op.drop_table("model_overrides")
