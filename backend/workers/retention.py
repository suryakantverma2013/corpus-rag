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
from app.db.session import get_sessionmaker
from app.services.checkpoint_retention import RetentionResult, prune_checkpoints

log = structlog.get_logger(__name__)

__all__ = ["prune_checkpoint_history"]


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
