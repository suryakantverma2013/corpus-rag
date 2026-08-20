"""Checkpoint retention (OI-30, R-65).

The checkpoint tables are langgraph's, not ours, so these seed them with raw SQL — which is
also the honest thing to do: what is under test is the SQL, and going through an ORM model
we do not own would test a fiction.

The property that matters most is the **blob** one. `checkpoint_blobs` is keyed
`(thread_id, checkpoint_ns, channel, version)` with no `checkpoint_id`, so blobs are shared
by every checkpoint naming that version — measured on real data, the second-newest
checkpoint shared 33 of 38 pairs with the newest. A prune that deleted "the old
checkpoint's blobs" would corrupt the checkpoint it just kept, and nothing else in the
suite would notice.

These assert on the **thread each test owns**, never on global counters: the development
database already carries orphaned threads — the very leak this module collects — so a
suite that expected a clean table would be flaky for the same reason the feature exists.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.models.conversation import Conversation
from app.db.repositories.users import UserRepository
from app.services.checkpoint_retention import _ORPHAN_THREADS, prune_checkpoints

_NS = ""


def _cid(n: int) -> str:
    """A time-ordered checkpoint id. Real ones are UUID6; ordering is all that matters."""
    return f"{n:032d}"


async def _seed_checkpoint(
    session: AsyncSession,
    thread: str,
    n: int,
    *,
    age_seconds: float,
    channel_versions: dict[str, str],
) -> None:
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    await session.execute(
        text(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id,"
            " parent_checkpoint_id, type, checkpoint, metadata)"
            " VALUES (:t, :ns, :id, NULL, 'msgpack', CAST(:cp AS jsonb), '{}'::jsonb)"
        ),
        {
            "t": thread,
            "ns": _NS,
            "id": _cid(n),
            "cp": json.dumps({"ts": ts, "channel_versions": channel_versions}),
        },
    )


async def _seed_blob(session: AsyncSession, thread: str, channel: str, version: str) -> None:
    await session.execute(
        text(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)"
            " VALUES (:t, :ns, :c, :v, 'msgpack', :b)"
        ),
        {"t": thread, "ns": _NS, "c": channel, "v": version, "b": b"x"},
    )


async def _seed_write(session: AsyncSession, thread: str, n: int) -> None:
    await session.execute(
        text(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id,"
            " idx, channel, type, blob) VALUES (:t, :ns, :id, :task, 0, 'ch', 'msgpack', :b)"
        ),
        {"t": thread, "ns": _NS, "id": _cid(n), "task": str(uuid.uuid4()), "b": b"x"},
    )


async def _qualifying_orphans(session: AsyncSession, *, min_age: float) -> int:
    """How many threads the orphan sweep would consider right now.

    Reuses the production predicate deliberately. A count written by hand here would stop
    agreeing with the sweep the moment that predicate changed, which is the whole failure
    this helper exists to prevent.
    """
    rows = await session.execute(text(_ORPHAN_THREADS), {"min_age": min_age, "limit": 10_000_000})
    return len(rows.all())


async def _count(session: AsyncSession, table: str, thread: str) -> int:
    return (
        await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE thread_id = :t"), {"t": thread}
        )
    ).scalar_one()


async def _live_conversation(session: AsyncSession) -> str:
    """A conversation whose thread must survive the orphan sweep (FR-PER-02: id == thread)."""
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@corpus.test"
    )
    convo = Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="live")
    session.add(convo)
    await session.flush()
    return str(convo.id)


# --- superseded checkpoints ---------------------------------------------------


async def test_keeps_the_newest_n_and_deletes_the_rest(session: AsyncSession) -> None:
    thread = await _live_conversation(session)
    for n in range(1, 11):
        await _seed_checkpoint(session, thread, n, age_seconds=7200, channel_versions={})

    await prune_checkpoints(session, keep=3, min_age_seconds=60, orphan_batch=100)

    survivors = (
        await session.execute(
            text("SELECT checkpoint_id FROM checkpoints WHERE thread_id=:t ORDER BY 1"),
            {"t": thread},
        )
    ).scalars()
    assert list(survivors) == [_cid(8), _cid(9), _cid(10)]


async def test_recent_checkpoints_are_never_pruned(session: AsyncSession) -> None:
    """The age floor, not `keep`, is what makes this safe beside a live turn.

    A burst of supersteps would otherwise push the checkpoint a resume needs out of the
    window while the run that wrote it is still going.
    """
    thread = await _live_conversation(session)
    for n in range(1, 11):
        await _seed_checkpoint(session, thread, n, age_seconds=5, channel_versions={})

    await prune_checkpoints(session, keep=3, min_age_seconds=3600, orphan_batch=100)

    assert await _count(session, "checkpoints", thread) == 10


async def test_writes_follow_their_checkpoint(session: AsyncSession) -> None:
    thread = await _live_conversation(session)
    for n in range(1, 6):
        await _seed_checkpoint(session, thread, n, age_seconds=7200, channel_versions={})
        await _seed_write(session, thread, n)

    await prune_checkpoints(session, keep=2, min_age_seconds=60, orphan_batch=100)

    assert await _count(session, "checkpoints", thread) == 2
    assert await _count(session, "checkpoint_writes", thread) == 2


# --- the blob property --------------------------------------------------------


async def test_a_blob_shared_with_a_surviving_checkpoint_is_kept(session: AsyncSession) -> None:
    """THE load-bearing test of this module.

    `shared` is named by both the oldest (doomed) and the newest (surviving) checkpoint.
    Deleting it would leave the survivor referencing a blob that no longer exists — a
    corrupted checkpoint, invisible until someone resumed that conversation.
    """
    thread = await _live_conversation(session)
    await _seed_checkpoint(
        session, thread, 1, age_seconds=7200, channel_versions={"shared": "v1", "stale": "v1"}
    )
    await _seed_checkpoint(
        session, thread, 2, age_seconds=7200, channel_versions={"shared": "v1", "fresh": "v2"}
    )
    await _seed_blob(session, thread, "shared", "v1")
    await _seed_blob(session, thread, "stale", "v1")
    await _seed_blob(session, thread, "fresh", "v2")

    await prune_checkpoints(session, keep=1, min_age_seconds=60, orphan_batch=100)

    remaining = set(
        (
            await session.execute(
                text("SELECT channel FROM checkpoint_blobs WHERE thread_id=:t"), {"t": thread}
            )
        ).scalars()
    )
    assert remaining == {"shared", "fresh"}, "a blob the survivor still references was deleted"


async def test_a_superseded_version_of_a_still_live_channel_is_collected(
    session: AsyncSession,
) -> None:
    """The other half of the blob predicate: match on channel **and** version.

    This is the ordinary case — a channel whose value changed, so the same channel exists at
    two versions. Matching on channel alone would keep the old blob forever because the
    channel is still named by the survivor, and the leak this whole module exists to stop
    would simply continue with the tests green. Found by mutation testing: dropping
    `cv.version = b.version` broke nothing until this test existed.
    """
    thread = await _live_conversation(session)
    await _seed_checkpoint(session, thread, 1, age_seconds=7200, channel_versions={"query": "v1"})
    await _seed_checkpoint(session, thread, 2, age_seconds=7200, channel_versions={"query": "v2"})
    await _seed_blob(session, thread, "query", "v1")
    await _seed_blob(session, thread, "query", "v2")

    await prune_checkpoints(session, keep=1, min_age_seconds=60, orphan_batch=1000)

    versions = set(
        (
            await session.execute(
                text("SELECT version FROM checkpoint_blobs WHERE thread_id=:t"), {"t": thread}
            )
        ).scalars()
    )
    assert versions == {"v2"}, "the superseded version of a live channel was not collected"


async def test_blobs_of_another_thread_are_untouched(session: AsyncSession) -> None:
    """The blob predicate is thread-scoped; a shared channel name must not cross threads."""
    doomed = await _live_conversation(session)
    other = await _live_conversation(session)
    await _seed_checkpoint(session, doomed, 1, age_seconds=7200, channel_versions={})
    await _seed_blob(session, doomed, "query", "v1")
    await _seed_checkpoint(session, other, 1, age_seconds=7200, channel_versions={"query": "v1"})
    await _seed_blob(session, other, "query", "v1")

    await prune_checkpoints(session, keep=1, min_age_seconds=60, orphan_batch=100)

    assert await _count(session, "checkpoint_blobs", other) == 1


# --- orphaned threads ---------------------------------------------------------


async def test_a_thread_with_no_conversation_is_removed_entirely(session: AsyncSession) -> None:
    """Possible at all because the checkpointer writes on its own autocommit pool (R-42(8)),
    so its rows never participate in the application's transactions."""
    orphan = str(uuid.uuid4())
    await _seed_checkpoint(session, orphan, 1, age_seconds=7200, channel_versions={"c": "v1"})
    await _seed_blob(session, orphan, "c", "v1")
    await _seed_write(session, orphan, 1)

    # `orphan_batch` is a throughput knob, not part of the property under test, and
    # `_ORPHAN_THREADS` has no ORDER BY -- so a batch smaller than the development database's
    # standing orphan backlog decides by hash order whether this row is considered at all.
    # Size it past the field so the LIMIT cannot bind (T-222).
    competing = await _qualifying_orphans(session, min_age=60)
    await prune_checkpoints(session, keep=3, min_age_seconds=60, orphan_batch=competing + 10)

    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert await _count(session, table, orphan) == 0


async def test_a_live_conversations_thread_is_never_orphan_swept(session: AsyncSession) -> None:
    thread = await _live_conversation(session)
    await _seed_checkpoint(session, thread, 1, age_seconds=7200, channel_versions={})

    await prune_checkpoints(session, keep=3, min_age_seconds=60, orphan_batch=1000)

    assert await _count(session, "checkpoints", thread) == 1


async def test_a_recent_orphan_is_left_alone(session: AsyncSession) -> None:
    """Covers the crash between creating a conversation row and the first checkpoint, and a
    conversation deleted while its final turn is still writing."""
    orphan = str(uuid.uuid4())
    await _seed_checkpoint(session, orphan, 1, age_seconds=5, channel_versions={})

    await prune_checkpoints(session, keep=3, min_age_seconds=3600, orphan_batch=1000)

    assert await _count(session, "checkpoints", orphan) == 1


# --- contract -----------------------------------------------------------------


async def test_keep_below_one_is_refused(session: AsyncSession) -> None:
    """Keeping zero deletes the checkpoint a resume needs. Refuse rather than clamp."""
    with pytest.raises(ValueError, match="keep must be >= 1"):
        await prune_checkpoints(session, keep=0, min_age_seconds=60, orphan_batch=100)


async def test_a_second_pass_removes_nothing(session: AsyncSession) -> None:
    """Idempotent, so a cron that overlaps itself is harmless."""
    thread = await _live_conversation(session)
    for n in range(1, 8):
        await _seed_checkpoint(session, thread, n, age_seconds=7200, channel_versions={})

    await prune_checkpoints(session, keep=2, min_age_seconds=60, orphan_batch=1000)
    after_first = await _count(session, "checkpoints", thread)
    await prune_checkpoints(session, keep=2, min_age_seconds=60, orphan_batch=1000)

    assert after_first == 2
    assert await _count(session, "checkpoints", thread) == after_first
