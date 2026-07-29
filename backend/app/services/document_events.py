"""The live document-status channel's engine (T-210, FR-KBM-09, ruling R-41 §8.22).

The poll-and-diff loop behind ``GET /api/v1/documents/events``, kept out of the route so
it can be tested without opening a connection: the route owns SSE framing and
serialisation, this module owns *what changed*.

**Why polling and not a push from the worker (R-41(2)).** The obvious design — the worker
publishes on each `set_status`, the API subscribes — cannot satisfy the constraint T-212
placed on this task, because **a crash-stalled job publishes nothing by definition**. A
document whose worker was SIGKILLed sits in its last FR-ING-01 state for up to
``job_timeout + 10`` seconds (~15 minutes at the provisional 900 s), and the event that
would tell the user so is exactly the event that never arrives. Any push design therefore
needs a timer beside it, at which point the timer is doing the work. Polling also has no
missed-event semantics: the database is re-read every tick, so a dropped tick self-heals,
where a dropped publish leaves a row stale on screen forever with nothing to correct it.

The cost is one indexed query per open stream per tick, bounded by
``SSE_MAX_STREAMS_PER_USER`` and by :data:`MAX_TRACKED_DOCUMENTS`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import DocumentStatus
from app.db.repositories.documents import DocumentListing, DocumentRepository

# R-41(5). `stalled` answers "is anything still working on this?", so it covers exactly the
# states a worker moves *through*. `ACTIVE`/`FAILED` are terminal — nothing is coming, and
# saying so twice adds nothing. The deletion states are excluded on stronger grounds than
# that: R-39(7) parks a dead-lettered purge at `DELETING` deliberately, with the document
# already out of retrieval and its diagnostics on `GET /jobs/{id}`, and requires the row
# keep rendering as `Deleting` for as long as it persists. Marking it stalled would
# contradict that ruling on precisely the documents the ruling was written for.
IN_FLIGHT_STATUSES: frozenset[DocumentStatus] = frozenset(
    {
        DocumentStatus.UPLOADED,
        DocumentStatus.QUEUED,
        DocumentStatus.PARSING,
        DocumentStatus.CHUNKING,
        DocumentStatus.EMBEDDING,
        DocumentStatus.INDEXING,
    }
)

# Matches the `limit` ceiling R-40(5) puts on `GET /api/v1/documents`, so the stream can
# never track more documents than the list route can show. A knowledge base larger than
# this streams its newest page; the modal is paged either way.
MAX_TRACKED_DOCUMENTS = 500


@dataclass(frozen=True, slots=True)
class DocumentState:
    """One document as the channel sees it: the R-40(5) listing plus the derived flag."""

    listing: DocumentListing
    stalled: bool

    @property
    def document_id(self) -> uuid.UUID:
        return self.listing.document.id


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The full matching set. Sent on every connect, and never sent again on one stream."""

    documents: tuple[DocumentState, ...]


@dataclass(frozen=True, slots=True)
class DocumentChanged:
    """One document whose rendered state differs from what this stream last emitted."""

    state: DocumentState


@dataclass(frozen=True, slots=True)
class DocumentRemoved:
    """A document that left the caller's set.

    Required because a tombstone does not *transition* into a visible state — the
    repository's `deleted_at IS NULL` predicate simply stops returning it, so
    disappearance is the only signal the GUI ever gets that a deletion completed.
    """

    document_id: uuid.UUID


DocumentEvent = Snapshot | DocumentChanged | DocumentRemoved

# Every field the R-40(5) DTO renders, plus `stalled`. Compared as a whole rather than
# tracked field by field so that adding a field to the DTO cannot silently produce a
# surface that shows something the stream never updates.
_Fingerprint = tuple[object, ...]


def is_stalled(listing: DocumentListing, *, stall_after: float, now: datetime) -> bool:
    """Has an in-flight document gone quiet for longer than `stall_after` seconds?

    `updated_at` moves on every one of the six FR-ING-01 stage writes, so silence here
    genuinely means no worker has touched the row — which is what T-212 measured: arq
    refuses to re-pick a job while `arq:in-progress:{job_id}` lives, so a SIGKILLed
    ingestion sits untouched for the remainder of that TTL.
    """
    if listing.document.status not in IN_FLIGHT_STATUSES:
        return False
    return (now - listing.document.updated_at).total_seconds() > stall_after


def _fingerprint(state: DocumentState) -> _Fingerprint:
    """The rendered state of one row.

    **Not `updated_at` alone.** `func.now()` is the *transaction* timestamp, so two writes
    in one transaction carry identical stamps and a cursor built on it would miss the
    second. Comparing the rendered fields sidesteps the question entirely: if nothing the
    user can see changed, nothing is emitted, whatever the timestamps say.
    """
    document = state.listing.document
    return (
        document.status,
        document.current_version,
        document.searchable,
        document.chunk_count,
        document.page_count,
        document.size_bytes,
        document.filename,
        document.mime_type,
        document.error_message,
        document.updated_at,
        state.listing.latest_job_id,
        state.listing.latest_job_error_code,
        state.listing.latest_job_document_version,
        state.stalled,
    )


async def _read_states(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    owner_id: uuid.UUID,
    knowledge_base_id: uuid.UUID | None,
    stall_after: float,
) -> list[DocumentState]:
    """One tick's read, in its own short-lived session.

    **A fresh session per tick, never the request-scoped one (R-41(7)).** A stream that
    held its injected `DbSession` would pin a connection-pool slot for as long as the
    modal stayed open, and a handful of viewers would exhaust the pool — the API would
    stop answering ordinary requests because some tabs were idle.

    The returned rows therefore **outlive the session that loaded them**: the caller
    fingerprints them and the route serialises them long after this closes. They are
    expunged before return so that is explicit — a detached instance keeps every column
    the SELECT loaded, and any attribute that is *not* loaded raises immediately here
    rather than becoming a lazy load in a sync function, where SQLAlchemy's async layer
    can only fail (`MissingGreenlet`). Every field the DTO and the fingerprint read is a
    plain column on that SELECT, so nothing is left to load.
    """
    async with sessionmaker() as session:
        listings = await DocumentRepository(session).list_for_owner(
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            limit=MAX_TRACKED_DOCUMENTS,
        )
        now = datetime.now(UTC)
        states = [
            DocumentState(
                listing=listing, stalled=is_stalled(listing, stall_after=stall_after, now=now)
            )
            for listing in listings
        ]
        session.expunge_all()
        return states


async def stream_document_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    owner_id: uuid.UUID,
    knowledge_base_id: uuid.UUID | None = None,
    poll_interval: float,
    stall_after: float,
    max_ticks: int | None = None,
) -> AsyncIterator[DocumentEvent]:
    """Yield a `Snapshot`, then one event per change, until the consumer stops iterating.

    Scoping is the repository's (`owner_id` is a query predicate, NFR-SEC-06) — this loop
    never filters in Python, so there is no path by which a caller sees another user's
    document.

    `max_ticks` exists for tests; production passes `None` and the loop ends only when the
    client disconnects and the consumer closes the generator.
    """
    seen: dict[uuid.UUID, _Fingerprint] = {}
    tick = 0

    while max_ticks is None or tick < max_ticks:
        states = await _read_states(
            sessionmaker,
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            stall_after=stall_after,
        )
        current = {state.document_id: state for state in states}

        if tick == 0:
            yield Snapshot(documents=tuple(states))
        else:
            for document_id, state in current.items():
                if seen.get(document_id) != _fingerprint(state):
                    yield DocumentChanged(state=state)
            for document_id in seen.keys() - current.keys():
                yield DocumentRemoved(document_id=document_id)

        seen = {document_id: _fingerprint(state) for document_id, state in current.items()}
        tick += 1

        if max_ticks is None or tick < max_ticks:
            await asyncio.sleep(poll_interval)


class TooManyStreamsError(Exception):
    """The caller already holds `SSE_MAX_STREAMS_PER_USER` open streams."""


class StreamRegistry:
    """Per-user concurrent-stream accounting (R-41(7)).

    Polling makes every open stream a recurring query, so an unbounded count is an
    unbounded query rate — a user with a dozen forgotten tabs would multiply this
    surface's cost by twelve without meaning to.

    **Per process, deliberately.** A shared counter in Redis would be exact across
    replicas but would put a network round-trip in front of every connect, and the cap is
    a guard rail rather than a quota: N replicas allowing N x cap is the right amount of
    wrong for something whose only job is to stop one user's tabs from multiplying.
    """

    def __init__(self) -> None:
        self._counts: dict[uuid.UUID, int] = {}

    def acquire(self, user_id: uuid.UUID, *, limit: int) -> None:
        if self._counts.get(user_id, 0) >= limit:
            raise TooManyStreamsError
        self._counts[user_id] = self._counts.get(user_id, 0) + 1

    def release(self, user_id: uuid.UUID) -> None:
        remaining = self._counts.get(user_id, 0) - 1
        if remaining > 0:
            self._counts[user_id] = remaining
        else:
            # Drop the key rather than leaving a zero behind: the dict is keyed by user and
            # would otherwise grow for the process's lifetime.
            self._counts.pop(user_id, None)

    def count(self, user_id: uuid.UUID) -> int:
        return self._counts.get(user_id, 0)

    def reset(self) -> None:
        """Test hook — mirrors the rate limiter's per-test reset in `conftest`."""
        self._counts.clear()


registry = StreamRegistry()
