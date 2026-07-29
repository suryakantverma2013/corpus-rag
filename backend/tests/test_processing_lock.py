"""The R-24 processing gate at the storage layer (T-302, R-43).

DB-backed, against the real `processing_locks` table, because the three properties that
make the gate correct are all properties of the *statements*: the upsert must be atomic,
the release must compare and delete in one statement, and the expiry must be the database's
clock rather than the application's. A fake would assert none of them.

Expiry is exercised by writing `expires_at` in the past, never by sleeping — a test that
waits out a TTL is a test that is slow when it passes and flaky when it does not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.processing_lock import ProcessingLockRepository
from app.db.repositories.users import UserRepository
from app.services.processing_lock import (
    DatabaseProcessingLockStore,
    MemoryProcessingLockStore,
    new_token,
)


async def _owner(session: AsyncSession) -> uuid.UUID:
    """A real `users` row — `processing_locks.owner_id` is a foreign key to it."""
    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=sub, email=f"{sub.hex[:8]}@corpus.local", display_name="User"
    )
    return sub


async def test_acquire_publishes_one_row_per_user(session: AsyncSession) -> None:
    owner = await _owner(session)
    repo = ProcessingLockRepository(session)
    conversation_id = None

    await repo.acquire(
        owner_id=owner, conversation_id=conversation_id, token="tok", ttl_seconds=180
    )

    held = await repo.active_for(owner)
    assert held is not None
    assert held.token == "tok"
    assert held.expires_at > datetime.now(UTC)
    assert held.expires_at > held.acquired_at


async def test_a_second_acquire_overwrites_rather_than_failing(session: AsyncSession) -> None:
    """Last writer wins — deliberately no `NX` (R-43(1)).

    Nothing serialises on this gate, so a failed acquire would leave the `lock` node
    choosing between blocking, erroring and proceeding unlocked. Overwriting is the only
    option of the four that keeps the gate held for as long as *any* turn is running.
    """
    owner = await _owner(session)
    repo = ProcessingLockRepository(session)

    await repo.acquire(owner_id=owner, conversation_id=None, token="first", ttl_seconds=180)
    await repo.acquire(owner_id=owner, conversation_id=None, token="second", ttl_seconds=180)

    rows = (
        await session.scalars(select(ProcessingLock).where(ProcessingLock.owner_id == owner))
    ).all()
    assert len(rows) == 1, "the primary key stopped being one row per user"
    assert rows[0].token == "second"


async def test_release_matches_on_the_token(session: AsyncSession) -> None:
    """The compare-and-delete is what stops a stale turn freeing a live one's gate."""
    owner = await _owner(session)
    repo = ProcessingLockRepository(session)
    await repo.acquire(owner_id=owner, conversation_id=None, token="live", ttl_seconds=180)

    assert await repo.release(owner_id=owner, token="stale") is False
    assert await repo.active_for(owner) is not None

    assert await repo.release(owner_id=owner, token="live") is True
    assert await repo.active_for(owner) is None


async def test_releasing_an_absent_lock_is_a_no_op(session: AsyncSession) -> None:
    """`finalize` runs on paths where `lock` never did, and must not care."""
    owner = await _owner(session)
    assert await ProcessingLockRepository(session).release(owner_id=owner, token="x") is False


async def test_an_expired_row_is_not_a_lock(session: AsyncSession) -> None:
    """`expires_at` is the crash release, and there is no sweeper (R-43(1)).

    A run killed between acquire and `finalize` leaves exactly this row. It must gate
    nothing, and the next turn must be able to overwrite it.
    """
    owner = await _owner(session)
    repo = ProcessingLockRepository(session)
    session.add(
        ProcessingLock(
            owner_id=owner,
            conversation_id=None,
            token="orphan",
            acquired_at=datetime.now(UTC) - timedelta(hours=1),
            expires_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    await session.flush()

    assert await repo.active_for(owner) is None

    await repo.acquire(owner_id=owner, conversation_id=None, token="fresh", ttl_seconds=180)
    held = await repo.active_for(owner)
    assert held is not None and held.token == "fresh"


async def test_the_gate_is_scoped_to_one_user(session: AsyncSession) -> None:
    """FR-STA-02 gates "the requesting user's GUI" — never anyone else's."""
    mine = await _owner(session)
    theirs = await _owner(session)
    repo = ProcessingLockRepository(session)

    await repo.acquire(owner_id=mine, conversation_id=None, token="t", ttl_seconds=180)

    assert await repo.active_for(mine) is not None
    assert await repo.active_for(theirs) is None


async def test_the_conversation_id_rides_along_as_a_diagnostic(session: AsyncSession) -> None:
    """Not part of the key, but carried so a 409 can say *which* chat is busy."""
    from app.db.base import DEFAULT_TENANT_ID
    from app.db.models.conversation import Conversation

    owner = await _owner(session)
    conversation = Conversation(owner_id=owner, tenant_id=DEFAULT_TENANT_ID, title="Chat")
    session.add(conversation)
    await session.flush()

    repo = ProcessingLockRepository(session)
    await repo.acquire(owner_id=owner, conversation_id=conversation.id, token="t", ttl_seconds=180)

    held = await repo.active_for(owner)
    assert held is not None and held.conversation_id == conversation.id


async def test_the_store_seam_round_trips_through_its_own_session(
    session: AsyncSession,
    db_connection,  # noqa: ANN001 — the conftest connection fixture
) -> None:
    """`DatabaseProcessingLockStore` opens and commits its own session, by design.

    A graph run outlives its HTTP request while streaming and has no request at all on
    resume, and an uncommitted row gates nothing — so this is one of the few services that
    commits rather than leaving it to a caller. Bound here to the test connection, so the
    commit is a savepoint release inside `conftest`'s rolled-back outer transaction.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner = await _owner(session)
    await session.flush()

    sessionmaker = async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    store = DatabaseProcessingLockStore(sessionmaker=sessionmaker, ttl_seconds=180)
    token = new_token()

    await store.acquire(owner_id=owner, conversation_id=None, token=token)
    assert await ProcessingLockRepository(session).active_for(owner) is not None

    assert await store.release(owner_id=owner, token=token) is True
    assert await ProcessingLockRepository(session).active_for(owner) is None


async def test_tokens_are_unique() -> None:
    """Uniqueness, not unguessability, is what the token is for."""
    assert len({new_token() for _ in range(256)}) == 256


async def test_the_memory_double_matches_the_store_protocol() -> None:
    """It is injection-only — no setting selects it — but it must behave like the real one."""
    from app.services.processing_lock import ProcessingLockStore

    double = MemoryProcessingLockStore()
    assert isinstance(double, ProcessingLockStore)

    owner = uuid.uuid4()
    await double.acquire(owner_id=owner, conversation_id=None, token="a")
    assert await double.release(owner_id=owner, token="b") is False
    assert await double.release(owner_id=owner, token="a") is True
