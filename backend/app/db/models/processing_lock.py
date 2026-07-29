"""`processing_locks` — the R-24 per-user action gate (T-302, R-43).

FR-STA-02 disables mutating file operations "while a response is being generated"; R-24
rescoped that to an **action-initiation gate on the requesting user's** upload / delete /
retry / replace, not a global KB freeze. This table is that gate made visible across
processes: the graph's `lock` node writes a row, the four mutating document routes read
it, and `finalize` deletes it.

Four properties, each deliberate:

1. **One row per user, `owner_id` as the primary key.** Three of the four enforcement
   points carry no conversation id at all (`DELETE /{id}`, `/{id}/retry`, `/{id}/replace`
   name a document, and a `scope=global` upload names nothing), so a per-conversation key
   would be unevaluable exactly where the gate is most needed. Per-user is also the
   substantively correct scope: a GLOBAL document uploaded from chat B enters chat A's
   ambient FR-ORC-06 retrieval scope mid-turn, which is the precise mutation FR-STA-02
   exists to prevent. `conversation_id` rides along as a diagnostic so the 409 and the
   logs can say *which* chat is busy.
2. **`token`, not a boolean.** Two turns may overlap (a second tab, Regenerate over a live
   turn). Acquire is a last-writer-wins upsert and release is a token-matched delete, so
   the older turn's `finalize` is a no-op and the gate holds until the *last* turn ends,
   independently of the order they finish in. Without the token an expired-then-reacquired
   lock would be released by the wrong turn.
3. **`expires_at` is the crash release, and there is no sweeper.** A run killed between
   acquire and `finalize` leaves one row per user — bounded, and ignored by every reader
   via `expires_at > now()`. Any later turn overwrites it. This is also why the gate is
   not derived from langgraph's own pending-task state: `review` parks on `interrupt()`
   for as long as a human takes, and state with no expiry would lock that user out of
   their own uploads for the duration.
4. **Not a column on `conversations`.** T-108 overrode `Conversation.updated_at` to
   `clock_timestamp()` with `onupdate` and FR-SBR-03 orders the sidebar on it, so writing
   the lock there would silently reorder the sidebar at turn start.

The lock is **advisory** (R-43): no code may rely on it for correctness. R-24 places
consistency on FR-ING-04/05 + FR-RET-04 and citation validity on serve-time FR-CIT-06, so
an expiry mid-turn is a sanctioned degradation of a UX gate, not a bug.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessingLock(Base):
    __tablename__ = "processing_locks"

    #: The **requesting caller** (`RAGContext.owner_id`), never the conversation's owner —
    #: the two differ when an admin runs someone else's conversation, and FR-STA-02 gates
    #: "the requesting user's GUI".
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    #: Diagnostic only — not part of the key. `SET NULL` so T-401's conversation delete
    #: cannot trip this FK on a turn that is still in flight.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False)

    # No `CreatedAtMixin`: the row *is* timestamps, and `acquired_at` is rewritten by every
    # re-acquire, which a `created_at` server default would misreport.
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
