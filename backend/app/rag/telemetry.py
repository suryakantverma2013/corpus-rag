"""The FR-ORC-03 telemetry vocabulary (T-302, R-43; durable half T-604, R-79).

FR-ORC-03 requires that "telemetry logs request start/end/failure and tracks token
consumption and latency per request; the stats panel is updated on finalization", and
NFR-OBS-01 says the same in observability terms.

**R-43(5) ruled that the stats panel needs no telemetry store**, and that is unchanged:
every FR-ANL card reads `messages` or client state — DURATION is a client session timer;
MESSAGES counts `messages`; MODEL, CONTEXT WINDOW (R-30: history + query only), DEEPEVAL
AVG (`messages.evaluation`) and SOURCES REFERENCED (`messages.citations`) are all
`messages`. Not one card reads a telemetry record, so NFR-OBS-02's "displayed counts match
telemetry" holds by **identity** rather than by two stores kept in step.

**R-79 closes the gap R-43 recorded**: an *errored* turn writes no `messages` row (R-54(3)
— FR-ERR-04 copy in `messages` would be charged against the NFR-CAP-01 budget R-51(4)
derives from it) and a *denied* one is durable only via the NFR-SEC-08 audit trail, so
"telemetry logs request **failure**" had no durable subject at all. A closed turn now also
writes one `turn_telemetry` row (`app.db.models.turn_telemetry`), retained for
`TELEMETRY_RETENTION_DAYS` (NFR-OBS-04, settled at 90).

**One record, three sinks.** :class:`TurnRecord` is built once in `finalize` and handed to
the log event, the durable row and the optional OTel span. That is what makes NFR-OBS-02's
identity structural: three readings of one clock cannot disagree, where three call sites
each assembling their own arguments eventually do. It is a frozen dataclass of ids and
scalars, so — like `StageEvent` on the wire (R-54(2)) — **it has no field that could carry
payload text**, which is R-43(5)'s rule made unrepresentable rather than remembered.

The event names and their key set remain a fixed contract. Two rules hold them together:

1. **Spans pair.** `.start` fires only where a span is opened (`telemetry_start`), and
   `.end`/`.failure` fire only where one was. A denial never reaches `telemetry_start`, so
   it emits `.denied` on its own rather than an `.end` with no `.start`.
2. **`.failure` is reserved for `outcome == "error"`.** An abstention is a response (R-23),
   not an incident; reporting one would make every user with an empty knowledge base look
   like an outage. :meth:`TurnRecord.is_error` decides, so the two call sites cannot drift.
3. **No payload text, ever.** No query, no answer, no chunk text. FR-PER-03's principle
   applied to the log stream, where it matters more than in the checkpointer because logs
   leave the machine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

__all__ = [
    "EVENT_NAMES",
    "TURN_DENIED",
    "TURN_END",
    "TURN_FAILURE",
    "TURN_START",
    "TurnRecord",
    "turn_closed",
    "turn_denied",
    "turn_start",
]

TURN_START = "graph.turn.start"
TURN_END = "graph.turn.end"
TURN_FAILURE = "graph.turn.failure"
TURN_DENIED = "graph.turn.denied"

#: The closed set. Tests assert emitted events are drawn from it.
EVENT_NAMES = frozenset({TURN_START, TURN_END, TURN_FAILURE, TURN_DENIED})

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One closed turn, as the log event, the durable row and the span all see it.

    Every field is an id or a scalar. There is deliberately no `query`, `answer` or
    `detail` parameter: a sink cannot log payload text it was never handed, which is a
    stronger guarantee than a convention each call site has to honour.

    `started_at` is the wall-clock epoch `telemetry_start` recorded, kept because the OTel
    span needs a real start instant — a span stamped "now" for both ends reports a
    zero-duration turn. `latency_ms` stays the authority: the span's end is computed from
    `started_at + latency_ms`, never from a second reading of the clock.
    """

    conversation_id: uuid.UUID
    owner_id: uuid.UUID
    turn_index: int | None
    outcome: str | None
    latency_ms: int
    error_code: str | None = None
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    message_id: uuid.UUID | None = None
    started_at: float | None = None

    #: The FR-CIT-06(4) gate's structural coverage score (T-308) — `RAGState.groundedness`.
    #:
    #: **Recorded here and nowhere a user can see it** (T-609, R-80(1)). R-49(1) forbids
    #: writing this into `messages.evaluation`, and that prohibition is untouched: it is about
    #: a *user surface*, where a second number beside the DeepEval chips would put two
    #: measurements of one property in front of one reader (OI-34). This is the operator's
    #: store, and R-50(6) already sanctioned keeping the gate/judge disagreement as internal
    #: signal — it was emitted on `rag.eval.completed` and therefore lived only as long as the
    #: log line. Durable, it becomes the one thing that makes `GATE_MIN_GROUNDEDNESS`
    #: answerable from evidence: without it, the threshold that decides whether an answer is
    #: served is the single knob whose input was retained nowhere, so no amount of accumulated
    #: feedback could ever calibrate it.
    groundedness: float | None = None

    @property
    def is_error(self) -> bool:
        """Whether this turn closes as `.failure` rather than `.end` (rule 2 above)."""
        return self.outcome == "error"


def turn_start(*, conversation_id: uuid.UUID | str, turn_index: int | None) -> None:
    """§4.12 step 2 — open the span."""
    log.info(TURN_START, conversation_id=str(conversation_id), turn_index=turn_index)


def turn_closed(record: TurnRecord) -> None:
    """§4.12 step 6 / the CATCH — close the span, whichever way the turn ended.

    One function rather than two because the choice between `.end` and `.failure` is a
    property of the record (`is_error`), not of the caller. `finalize` previously decided it
    with an `if` and then assembled two different argument lists; the invariant "`.failure`
    is reserved for `outcome == 'error'`" was true only while those two branches agreed.
    """
    if record.is_error:
        log.warning(
            TURN_FAILURE,
            conversation_id=str(record.conversation_id),
            turn_index=record.turn_index,
            error_code=record.error_code,
            latency_ms=record.latency_ms,
        )
        return
    # `blocked`, `abstained` and `review` close as ends, not failures — see rule 2.
    log.info(
        TURN_END,
        conversation_id=str(record.conversation_id),
        turn_index=record.turn_index,
        outcome=record.outcome,
        latency_ms=record.latency_ms,
        model_name=record.model_name,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
    )


def turn_denied(
    *, conversation_id: uuid.UUID | str, owner_id: uuid.UUID | str, reason: str
) -> None:
    """FR-ORC-02 — a turn refused before any span was opened.

    Its own event rather than a `.failure`, because no `.start` preceded it and a
    span-pairing consumer would otherwise see an orphaned end on exactly the
    security-relevant turns. It writes **no `turn_telemetry` row** either, and that is the
    same argument one layer down: the table's invariant is one row per turn that *ran*, and
    a denial never opened a span, took the lock, chose a model or spent a token — a row for
    it would be NULL in every metric column the table exists to hold. Its durable home is
    the NFR-SEC-08 audit trail (R-43(7), foreign-owner only; a missing conversation is a
    resumed run whose chat was deleted, which is lifecycle rather than a security event).
    """
    log.warning(
        TURN_DENIED,
        conversation_id=str(conversation_id),
        owner_id=str(owner_id),
        reason=reason,
    )
