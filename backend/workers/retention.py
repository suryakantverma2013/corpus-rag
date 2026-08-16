"""Cron entrypoint for checkpoint retention (OI-30, R-65).

The policy and the SQL live in `app.services.checkpoint_retention`; this is only the arq
wrapper — the same split as `workers/sweeper.py`, so the pruning logic is testable without
a worker and reusable from a one-off script.

Why a cron rather than pruning at the end of each turn: the turn path is the latency-
sensitive one (NFR-PRF-02), the work is pure housekeeping nobody is waiting for, and a
per-turn prune would still leave orphaned threads uncollected — they have no turn.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.config import get_settings
from app.db.repositories.turn_telemetry import TurnTelemetryRepository
from app.db.session import get_sessionmaker
from app.services.checkpoint_retention import RetentionResult, prune_checkpoints

log = structlog.get_logger(__name__)

__all__ = ["prune_checkpoint_history", "prune_turn_telemetry"]


async def prune_checkpoint_history(ctx: dict[str, Any]) -> int:
    """Trim the checkpoint store. Returns the number of rows removed.

    **Never raises.** Retention is maintenance: a failed pass costs disk until the next
    one, while an exception here would mark the cron job failed and clutter the operator's
    view with an incident that has no user-visible effect. Same disposition as the R-50
    evaluation path — degraded output is "nothing pruned".
    """
    settings = ctx.get("settings") or get_settings()
    sessionmaker = ctx.get("sessionmaker") or get_sessionmaker()
    cfg = settings.checkpointer

    if cfg.retention_interval_seconds <= 0:
        # 0 is the documented off switch, and the scheduler should not have called us.
        return 0
    if cfg.backend != "postgres":
        # `memory` keeps nothing across a restart, so there is nothing to prune.
        return 0

    try:
        async with sessionmaker() as session:
            result: RetentionResult = await prune_checkpoints(
                session,
                keep=cfg.retention_keep,
                min_age_seconds=cfg.retention_min_age_seconds,
                orphan_batch=cfg.retention_orphan_batch,
            )
            await session.commit()
    except Exception:
        log.exception("checkpoint.retention.failed")
        return 0

    return result.total


async def prune_turn_telemetry(ctx: dict[str, Any]) -> int:
    """Trim `turn_telemetry` to the NFR-OBS-04 horizon. Returns the number of rows removed.

    **Never raises**, for the same reason as the checkpoint sweep above: retention is
    maintenance, and a failed pass costs disk until the next one where an exception marks a
    cron job failed and puts an incident in front of an operator that no user can see.

    `TELEMETRY_RETENTION_DAYS = 0` means **keep forever** and is checked here as well as at
    registration — the opposite polarity from `CHECKPOINTER_RETENTION_INTERVAL_SECONDS`, and
    it must be: read as a horizon, 0 is "delete everything older than now", the single value
    whose literal reading empties the table. Guarding it in both places is deliberate
    belt-and-braces on a destructive default.
    """
    settings = ctx.get("settings") or get_settings()
    sessionmaker = ctx.get("sessionmaker") or get_sessionmaker()
    cfg = settings.telemetry

    if cfg.retention_days <= 0:
        return 0

    try:
        async with sessionmaker() as session:
            removed = await TurnTelemetryRepository(session).prune(
                older_than_days=cfg.retention_days, batch=cfg.retention_batch
            )
            await session.commit()
    except Exception:
        log.exception("telemetry.retention.failed")
        return 0

    if removed:
        log.info("telemetry.retention.pruned", rows=removed, older_than_days=cfg.retention_days)
    return removed
