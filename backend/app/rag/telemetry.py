"""The FR-ORC-03 telemetry vocabulary (T-302, R-43).

FR-ORC-03 requires that "telemetry logs request start/end/failure and tracks token
consumption and latency per request; the stats panel is updated on finalization", and
NFR-OBS-01 says the same in observability terms. R-43 rules that for MVP this is satisfied
by **structured logs plus the metric columns already on `messages`** — no telemetry table:

* Every FR-ANL stats-panel card reads `messages` or client-side state. DURATION is a client
  session timer; MESSAGES counts `messages`; MODEL, CONTEXT WINDOW (R-30: history + query
  only), DEEPEVAL AVG (`messages.evaluation`) and SOURCES REFERENCED (`messages.citations`)
  are all `messages`. **Not one card reads a telemetry record**, so "the stats panel is
  updated on finalization" needs nothing beyond the row T-402 writes.
* NFR-OBS-02 requires displayed token counts to "match telemetry". If the displayed numbers
  *are* the columns telemetry reports, that is identity rather than agreement — a stronger
  guarantee than two stores kept in step.
* Durable per-request records already belong to **T-604** ("structlog + per-request
  telemetry records (NFR-OBS-01/02)", `deps: T-402`). Building them here would front-run a
  task that exists, and NFR-OBS-04 leaves telemetry retention an open TBD, so a durable
  store would create an obligation the spec has not settled.

Recorded consequence: a **denied** turn is durable via the NFR-SEC-08 audit trail, but an
**errored** turn is logs-only until T-604 — it never produces a `messages` row.

The event names and their key set are a **contract binding T-604**, which will bind a
durable sink to exactly these names. Two rules hold them together:

1. **Spans pair.** `.start` fires only where a span is opened (`telemetry_start`), and
   `.end`/`.failure` fire only where one was. A denial never reaches `telemetry_start`, so
   it emits `.denied` on its own rather than an `.end` with no `.start`.
2. **No payload text, ever.** No query, no answer, no chunk text. This is FR-PER-03's
   principle applied to the log stream, where it matters more than in the checkpointer
   because logs leave the machine. The helpers below take no parameter that could carry it.
"""

from __future__ import annotations

import uuid

import structlog

__all__ = [
    "EVENT_NAMES",
    "TURN_DENIED",
    "TURN_END",
    "TURN_FAILURE",
    "TURN_START",
    "turn_denied",
    "turn_end",
    "turn_failure",
    "turn_start",
]

TURN_START = "graph.turn.start"
TURN_END = "graph.turn.end"
TURN_FAILURE = "graph.turn.failure"
TURN_DENIED = "graph.turn.denied"

#: The closed set. Tests assert emitted events are drawn from it.
EVENT_NAMES = frozenset({TURN_START, TURN_END, TURN_FAILURE, TURN_DENIED})

log = structlog.get_logger(__name__)


def turn_start(*, conversation_id: uuid.UUID | str, turn_index: int | None) -> None:
    """§4.12 step 2 — open the span."""
    log.info(TURN_START, conversation_id=str(conversation_id), turn_index=turn_index)


def turn_end(
    *,
    conversation_id: uuid.UUID | str,
    turn_index: int | None,
    outcome: str | None,
    latency_ms: int,
    model_name: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """§4.12 step 6 — close the span for a non-error terminal.

    Covers `answered`, `abstained`, `blocked` and `review`. An abstention is a response
    (R-23), not an incident; emitting `.failure` for one would make every user with an empty
    knowledge base look like an outage.
    """
    log.info(
        TURN_END,
        conversation_id=str(conversation_id),
        turn_index=turn_index,
        outcome=outcome,
        latency_ms=latency_ms,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def turn_failure(
    *,
    conversation_id: uuid.UUID | str,
    turn_index: int | None,
    error_code: str | None,
    latency_ms: int,
    detail_code: str | None = None,
) -> None:
    """§4.12's CATCH — close the span for `outcome == "error"` only.

    `error_code` is the FR-ORC-05 class the user is shown; `detail_code` is the originating
    subsystem's own code, which the class deliberately collapses (see `app.rag.errors`).
    Keeping it here is what makes "few user-facing classes, rich operator detail" true.
    """
    log.warning(
        TURN_FAILURE,
        conversation_id=str(conversation_id),
        turn_index=turn_index,
        error_code=error_code,
        detail_code=detail_code,
        latency_ms=latency_ms,
    )


def turn_denied(
    *, conversation_id: uuid.UUID | str, owner_id: uuid.UUID | str, reason: str
) -> None:
    """FR-ORC-02 — a turn refused before any span was opened.

    Its own event rather than a `.failure`, because no `.start` preceded it and a
    span-pairing consumer (T-604) would otherwise see an orphaned end on exactly the
    security-relevant turns.
    """
    log.warning(
        TURN_DENIED,
        conversation_id=str(conversation_id),
        owner_id=str(owner_id),
        reason=reason,
    )
