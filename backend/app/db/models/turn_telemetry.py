"""`turn_telemetry` — the durable per-request record (NFR-OBS-01, T-604, R-79).

**Why this table exists at all, given R-43(5) ruled there should be none.** That ruling is
about the *stats panel*, and it still holds: no FR-ANL card reads a telemetry record, so
NFR-OBS-02's "displayed counts match telemetry" is an identity between what the panel shows
and what telemetry reports, not an agreement between two stores. What R-43(5) also recorded
was a gap it deferred here: **a turn that fails has no durable representation anywhere.**
An answered, abstained or blocked turn writes a `messages` row carrying `model_name`,
`prompt_tokens`, `completion_tokens` and `latency_ms`; a *denied* one is audited; an
**errored** one writes nothing at all, because R-54(3) keeps FR-ERR-04 copy out of
`messages` (it would be charged against the NFR-CAP-01 budget R-51(4) derives from that
table). So "telemetry logs request start/end/**failure**" was true of the log stream and of
nothing that survives a restart. One row per closed turn closes it.

**No foreign keys, deliberately.** `conversation_id`, `owner_id` and `message_id` are
plain UUIDs. A telemetry row is an operator's record of what the system did, and it must
outlive the conversation whose deletion is a user action — an error-rate or latency history
that a user can rewrite by clearing their chats is not a record. The rows hold **no payload
text** (the columns are ids, an outcome, a failure class and four numbers), so keeping them
past a conversation delete discloses nothing the audit trail does not already keep, and
`TELEMETRY_RETENTION_DAYS` bounds how long. *Revisit if a data-subject-erasure obligation
lands (OI-21's single-org disposition is what keeps that out of scope today) — the fix is a
purge by `owner_id`, not a foreign key, since `ON DELETE CASCADE` would reintroduce exactly
the user-rewritable history this avoids.*

**One row per turn that ran.** Written by `finalize` beside the `graph.turn.end` /
`graph.turn.failure` event, from the same :class:`~app.rag.telemetry.TurnRecord`. A denial
writes none — it never opened a span, took the lock, chose a model or spent a token, so its
row would be NULL in every metric column this table exists to hold (see
`telemetry.turn_denied`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class TurnTelemetry(CreatedAtMixin, Base):
    __tablename__ = "turn_telemetry"
    __table_args__ = (
        # The retention sweep's predicate (R-79(3)) and every "what happened in the last
        # hour" query.
        Index("ix_turn_telemetry_created_at", "created_at"),
        # One conversation's history — the join a support question starts from.
        Index("ix_turn_telemetry_conversation_id_created_at", "conversation_id", "created_at"),
        # Error-rate over time. `audit_log`'s `(event_type, created_at)` precedent: the
        # discriminating column first, the time range second.
        Index("ix_turn_telemetry_outcome_created_at", "outcome", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: == `conversations.id` == the LangGraph `thread_id` (FR-PER-02). Not a foreign key.
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The turn's *requesting caller* — the same principal the R-24 lock keys on (R-43(1)),
    #: which differs from the conversation's owner exactly on the `is_admin` path.
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: The question's rank in the transcript. Nullable because a resumed run can close
    #: without one, and a telemetry row is worth keeping even when it cannot be placed.
    turn_index: Mapped[int | None] = mapped_column(Integer)

    #: `answered` / `abstained` / `blocked` / `review` / `error`. Not an enum type: the
    #: terminal set is `RAGState`'s (R-42(2)), and a CHECK constraint here would make adding
    #: one a migration on a table nothing depends on.
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The FR-ORC-05 failure *class* (`app.rag.errors`), present only when `outcome` is
    #: `error`. Never `type(exc).__name__` — R-43(6) removed that from every durable store.
    error_code: Mapped[str | None] = mapped_column(String(64))

    #: The R-43(5) metric set. All nullable: an errored or blocked turn never reached
    #: generation, and a zero would claim a measurement that was not taken.
    model_name: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)

    #: Wall-clock `finalize − telemetry_start` (R-43(8)); the **same** integer written to
    #: `messages.latency_ms`, not a second reading — that identity is what NFR-OBS-02 asks
    #: for and it survives only while one value feeds both writes.
    #: Bound, recorded rather than guarded: `Integer` is int32, so a turn lasting more than
    #: ~24 days cannot be written and the row is lost (the write fails open, R-79(1)). Only a
    #: `review` turn parked on `interrupt()` could reach that, and R-49(6) makes `review`
    #: reserved and never emitted — so the case is unreachable today, and widening the column
    #: for it would be sizing a store for a state the graph cannot enter.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The FR-CIT-06(4) gate's structural coverage for this turn (T-609, R-80(1)).
    #:
    #: **Operator store only.** R-49(1) keeps this off every *user* surface and out of
    #: `messages.evaluation`, where it would put a second measurement of one property beside
    #: the DeepEval chips (OI-34); that ruling is unchanged. Here it is the input to the one
    #: threshold users' feedback most wants to calibrate — `GATE_MIN_GROUNDEDNESS`, which
    #: decides whether an answer is served at all — and before this it was retained nowhere,
    #: so the gate was the single knob no accumulated evidence could ever reach.
    #: `None` for a turn that never reached the gate (an error, an injection block).
    groundedness: Mapped[float | None] = mapped_column(Float)

    #: The `messages` row this turn produced, when it produced one. Absent for `error`
    #: (R-54(3)) and for `blocked`; present for `answered`/`abstained`. It is what makes the
    #: two stores joinable without either one owning the other.
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
