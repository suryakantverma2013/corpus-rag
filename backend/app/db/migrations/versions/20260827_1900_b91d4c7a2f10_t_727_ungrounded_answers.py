"""T-727 messages.ungrounded — the FR-MSG-09 answer that cites nothing (R-98)

One boolean, and its whole purpose is to be **queryable**. R-98(5) persists the ungrounded
answer so a reload does not lose it (FR-PER-01), and in the same breath withholds it from the
history the generator sees — otherwise an invented claim re-enters as trusted `assistant`
speech and can ground a later answer, which is OI-32's hazard made worse because this content
is fabricated by construction rather than merely possibly-poisoned.

`load_history` is therefore the one query that must filter on it, which is why this is a column
rather than a flag inside the existing `citations` payload: a JSONB probe would work and would
be slower and harder to read in exactly the place correctness depends on it.

**`NOT NULL DEFAULT FALSE`, and the default is the safe direction.** Every existing row is a
grounded answer or a user message, and anything the migration cannot classify must read as
"include me in history" — the alternative silently truncates the transcript of every
conversation in the corpus.

No index. The filter is always applied beside `conversation_id`, which is already indexed, and
the selectivity of a boolean that is false for nearly every row would not earn one.

Revision ID: b91d4c7a2f10
Revises: 402f3492aed5
Create Date: 2026-08-27 19:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b91d4c7a2f10"
down_revision: str | None = "402f3492aed5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "ungrounded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "FR-MSG-09 (R-98): answered from the model's own training, not from retrieved "
                "passages. Cites nothing, is never evaluated, and is excluded from the history "
                "supplied to the generator on later turns."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "ungrounded")
