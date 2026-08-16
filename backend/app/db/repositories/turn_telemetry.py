"""Turn-telemetry repository (T-604, R-79) — the durable NFR-OBS-01 record.

Write-mostly and append-only by convention, like `audit_log`: `record` inserts, `prune`
deletes by age, and nothing updates a row. The only read surface is `count_since`, which
exists so a test can assert what was written and so an operator has a supported way to ask
"how many turns, and how many failed" without hand-writing SQL against the table.

Like every repository this flushes and never commits — the caller owns the unit of work.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.db.models.turn_telemetry import TurnTelemetry
from app.db.repositories.base import BaseRepository
from app.rag.telemetry import TurnRecord


class TurnTelemetryRepository(BaseRepository[TurnTelemetry]):
    model = TurnTelemetry

    async def record(self, record: TurnRecord) -> TurnTelemetry:
        """Append one row from the same `TurnRecord` the log event was built from.

        Taking the record rather than a keyword list is the point: the row, the log line and
        the OTel span are then three renderings of one object, and NFR-OBS-02's identity is a
        property of the type rather than of three call sites agreeing.

        `outcome` is defaulted to `"error"` when the record carries an `error_code` and no
        outcome — the column is `NOT NULL` because a row that cannot say how the turn ended
        answers nothing anyone would query this table for, and the only path that can reach
        here without one is a failure whose class is already known.
        """
        outcome = record.outcome or ("error" if record.error_code else "unknown")
        row = TurnTelemetry(
            conversation_id=record.conversation_id,
            owner_id=record.owner_id,
            turn_index=record.turn_index,
            outcome=outcome,
            error_code=record.error_code,
            model_name=record.model_name,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            latency_ms=record.latency_ms,
            message_id=record.message_id,
            groundedness=record.groundedness,
        )
        return await self.add(row)

    async def prune(self, *, older_than_days: int, batch: int) -> int:
        """Delete rows older than the retention horizon. Returns how many went.

        Bounded by `batch` so one pass cannot lock a large slice of the table (R-65's
        `retention_orphan_batch` rule); the cron converges over successive runs rather than
        holding a long transaction once. The cutoff is computed here rather than in SQL so
        the caller — and a test — can reason about it without a database round trip.

        A `DELETE ... WHERE id IN (SELECT ... LIMIT n)` rather than a bare `LIMIT`, which
        Postgres does not accept on `DELETE`.
        """
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        doomed = (
            select(TurnTelemetry.id)
            .where(TurnTelemetry.created_at < cutoff)
            .order_by(TurnTelemetry.created_at)
            .limit(batch)
            .scalar_subquery()
        )
        result = await self.session.execute(
            delete(TurnTelemetry).where(TurnTelemetry.id.in_(doomed))
        )
        return result.rowcount or 0

    async def count_since(self, *, since: datetime, outcome: str | None = None) -> int:
        """How many turns closed since `since`, optionally of one outcome."""
        stmt = (
            select(func.count()).select_from(TurnTelemetry).where(TurnTelemetry.created_at >= since)
        )
        if outcome is not None:
            stmt = stmt.where(TurnTelemetry.outcome == outcome)
        return int((await self.session.scalar(stmt)) or 0)

    async def list_for_conversation(
        self, *, conversation_id: uuid.UUID, limit: int = 100
    ) -> Sequence[TurnTelemetry]:
        """Newest-first turns of one conversation — the support question's starting point."""
        stmt = (
            select(TurnTelemetry)
            .where(TurnTelemetry.conversation_id == conversation_id)
            .order_by(TurnTelemetry.created_at.desc(), TurnTelemetry.id.desc())
            .limit(limit)
        )
        return (await self.session.scalars(stmt)).all()
