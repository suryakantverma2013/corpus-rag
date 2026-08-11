"""Checkpoint retention — bounding the FR-PER-01 store (OI-30, R-65).

LangGraph writes one checkpoint per superstep and keeps every one. R-42(11) makes T-401
delete a conversation's whole thread on `DELETE /conversations/{id}`, which closes the
privacy hole, but nothing prunes the *superseded* checkpoints of a conversation that is
still alive. Measured on the dev database before this shipped: **7,489 checkpoints,
33,903 write rows, 43 MB** across 547 threads, with ~24 checkpoints for a one-to-two turn
conversation. It only grows, and **only the latest is ever read** — nothing in the tree
calls `aget_state_history`.

Two distinct leaks, one job:

1. **Superseded checkpoints of live threads.** Pruned to the newest `keep` per thread.
2. **Orphaned threads.** Checkpoint rows whose conversation no longer exists. These are
   possible at all because the checkpointer writes through its own psycopg pool with
   `autocommit` (R-42(8)), so its writes never participate in the application's
   transactions — a conversation removed by anything other than the delete route leaves
   its thread behind with nothing to collect it.

**What this reclaims, precisely.** Measured on the dev database: 8,031 → 813 checkpoints
and 36,451 → 3,822 write rows, ~90% of both, converging in three batch-limited passes. The
**on-disk size does not drop**, and that is expected rather than a defect: Postgres marks
the tuples dead and autovacuum returns the space *for reuse*, so the tables stop growing but
the files do not shrink without a `VACUUM FULL`, which takes an exclusive lock and is an
operator's decision, not a cron's. The win is bounded growth, not a smaller file today.

**The blob table is why this is not a one-line DELETE.** `checkpoint_blobs` is keyed
`(thread_id, checkpoint_ns, channel, version)` and carries **no `checkpoint_id`** — blobs
are shared by every checkpoint whose `channel_versions` names that version. Measured on a
real thread, the second-newest checkpoint shared **33 of 38** channel/version pairs with
the newest, so deleting "the blobs of an old checkpoint" would corrupt the checkpoint we
just kept. Blobs are therefore removed by **set difference against the survivors**, never
per checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

__all__ = ["RetentionResult", "prune_checkpoints"]


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What one pass removed. Zero everywhere is the steady state, not a failure."""

    orphan_threads: int = 0
    checkpoints: int = 0
    writes: int = 0
    blobs: int = 0

    @property
    def total(self) -> int:
        return self.orphan_threads + self.checkpoints + self.writes + self.blobs


# Threads with no `conversations` row. Safe as a join because R-42(8) derives the
# checkpointer DSN from `DATABASE_URL`: the checkpoint tables and the application tables
# are the same database by construction, not by coincidence.
#
# The age floor is not belt-and-braces. T-401 creates the conversation row before the graph
# runs, so a thread without one is genuinely orphaned — but a crash between those two steps,
# or a conversation deleted while its final turn is still writing, would otherwise be
# collected while a run still holds it.
_ORPHAN_THREADS = """
SELECT DISTINCT c.thread_id
FROM checkpoints c
WHERE NOT EXISTS (SELECT 1 FROM conversations v WHERE v.id::text = c.thread_id)
  AND NOT EXISTS (
      SELECT 1 FROM checkpoints n
      WHERE n.thread_id = c.thread_id
        AND (n.checkpoint->>'ts')::timestamptz >= now() - make_interval(secs => :min_age)
  )
LIMIT :limit
"""

_DELETE_THREAD = "DELETE FROM {table} WHERE thread_id = ANY(:threads)"

# Superseded checkpoints. `checkpoint_id` is time-ordered (verified against the `ts` field:
# the two orderings disagreed on zero rows), so ranking on it needs no jsonb extraction.
#
# The age floor is what protects an in-flight run: a turn mid-superstep is writing
# checkpoints right now, and `keep` alone would let a burst of supersteps push the one a
# resume needs out of the window.
_PRUNE_CHECKPOINTS = """
WITH ranked AS (
    SELECT thread_id, checkpoint_ns, checkpoint_id,
           row_number() OVER (
               PARTITION BY thread_id, checkpoint_ns ORDER BY checkpoint_id DESC
           ) AS rn
    FROM checkpoints
)
DELETE FROM checkpoints c
USING ranked r
WHERE c.thread_id = r.thread_id
  AND c.checkpoint_ns = r.checkpoint_ns
  AND c.checkpoint_id = r.checkpoint_id
  AND r.rn > :keep
  AND (c.checkpoint->>'ts')::timestamptz < now() - make_interval(secs => :min_age)
"""

# Writes belong to exactly one checkpoint, so they follow it directly.
_PRUNE_WRITES = """
DELETE FROM checkpoint_writes w
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.thread_id = w.thread_id
      AND c.checkpoint_ns = w.checkpoint_ns
      AND c.checkpoint_id = w.checkpoint_id
)
"""

# Blobs do not. This is the set difference described in the module docstring: a blob row
# survives if ANY remaining checkpoint of its thread still names that (channel, version).
# Run *after* the checkpoint prune, so "remaining" means what we kept.
_PRUNE_BLOBS = """
DELETE FROM checkpoint_blobs b
WHERE NOT EXISTS (
    SELECT 1
    FROM checkpoints c,
         LATERAL jsonb_each_text(c.checkpoint->'channel_versions') AS cv(channel, version)
    WHERE c.thread_id = b.thread_id
      AND c.checkpoint_ns = b.checkpoint_ns
      AND cv.channel = b.channel
      AND cv.version = b.version
)
"""


async def prune_checkpoints(
    session: AsyncSession,
    *,
    keep: int,
    min_age_seconds: float,
    orphan_batch: int,
) -> RetentionResult:
    """Delete one batch of superseded checkpoints and orphaned threads.

    Idempotent and safe to run concurrently with live turns: nothing newer than
    ``min_age_seconds`` is touched, and the newest ``keep`` checkpoints of every thread are
    retained regardless of age.

    Ordering is load-bearing. Orphaned threads go first so their rows are not also ranked
    by the prune; blobs go **last**, because the set of surviving `channel_versions` is only
    correct once the checkpoints that are going have gone.

    The caller commits. This function does no transaction control of its own so a scheduler
    can wrap the whole pass, and so tests can roll it back.
    """
    if keep < 1:
        # Keeping zero would delete the checkpoint a resume needs. Refuse rather than
        # clamp: a configuration that asks for it is wrong about what this store is for.
        msg = f"checkpoint retention keep must be >= 1, got {keep}"
        raise ValueError(msg)

    orphans = list(
        (
            await session.execute(
                text(_ORPHAN_THREADS), {"min_age": min_age_seconds, "limit": orphan_batch}
            )
        ).scalars()
    )
    if orphans:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await session.execute(text(_DELETE_THREAD.format(table=table)), {"threads": orphans})

    pruned = await session.execute(
        text(_PRUNE_CHECKPOINTS), {"keep": keep, "min_age": min_age_seconds}
    )
    writes = await session.execute(text(_PRUNE_WRITES))
    blobs = await session.execute(text(_PRUNE_BLOBS))

    result = RetentionResult(
        orphan_threads=len(orphans),
        checkpoints=pruned.rowcount or 0,
        writes=writes.rowcount or 0,
        blobs=blobs.rowcount or 0,
    )
    if result.total:
        log.info(
            "checkpoint.retention.pruned",
            orphan_threads=result.orphan_threads,
            checkpoints=result.checkpoints,
            writes=result.writes,
            blobs=result.blobs,
        )
    return result
