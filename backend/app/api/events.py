"""The SSE frame base and the two stream vocabularies (T-405, R-57).

Three endpoints stream, six event names cross the wire, and until this task **none of them had
a type** — every frame was a `json.dumps(...)` literal built at the yield site, invisible to
the OpenAPI document and therefore to the GUI that has to parse it.

FastAPI 0.139's native SSE (`fastapi.sse`) types a stream from the handler's return annotation:
``-> AsyncIterator[ChatStreamFrame]`` publishes the union as a **named component** with every
member beside it, which is exactly what `openapi-typescript` needs to emit a discriminated
union. T-508 still hand-rolls the *transport* — R-41(3) requires `fetch` + `ReadableStream`,
since the stream authenticates with an ordinary `Authorization` header and `EventSource` cannot
send one — but it now hand-writes **no types at all**, not even an event-name→payload map.

**The frame is an envelope, and that is forced rather than chosen.** `data:` carries
``{"event": ..., "data": {...}}`` rather than the bare payload, because the three chat payloads
are not mutually discriminable — `done`'s ``{outcome}`` is a subset of `message`'s keys — so a
union with no in-payload tag could never be narrowed by a client. The SSE ``event:`` line is
kept as well (R-41 names the three document events, and T-508 is written against them), so the
name appears twice. The redundancy is deliberate: a parser may switch on the JSON alone and
never correlate two lines of the wire format.

**Yielding a `ServerSentEvent` skips `stream_item_field` validation** — FastAPI lets a handler
mix types on purpose. So the declared union governs the *schema* and not the runtime, and
:meth:`SseFrame.to_event` is the only sanctioned path to the wire: it takes the event name from
the model, so the ``event:`` line and the payload's own `event` key cannot disagree.
`test_openapi_contract.py` asserts every builder returns a declared frame.

The two vocabularies are restated here rather than imported. `app.rag.state.Outcome` is a plain
`Literal` assignment, which the schema **inlines**; a PEP-695 alias gets a named component, and
retyping it in `state.py` would touch a field of `RAGState` — a contract R-42(2) freezes and
T-406's drift guards read. Restating costs a test, which is cheaper than editing that contract
for a cosmetic reason.
"""

from __future__ import annotations

from typing import Literal

from fastapi.sse import ServerSentEvent
from pydantic import BaseModel

__all__ = ["SseFrame", "TurnOutcome", "TurnStage"]

#: Coarse progress, as `app.services.chat._STAGES` maps it. Several graph nodes collapse onto
#: one stage on purpose: FR-MSG-05's affordance is a typing indicator, not a progress bar, and
#: naming `rerank` separately would leak the pipeline's shape to a client that must not depend
#: on it. Guarded by `test_the_stage_vocabulary_matches_the_chat_service`.
type TurnStage = Literal["preparing", "retrieving", "generating", "verifying"]

#: How a turn ended. Mirrors `app.rag.state.Outcome`; `review` is **reserved and never emitted**
#: (R-49(6)) and is carried here only so the two stay the same closed set.
type TurnOutcome = Literal["answered", "abstained", "blocked", "error", "review"]


class SseFrame(BaseModel):
    """One declared frame, and the only sanctioned way one reaches the wire.

    Subclasses narrow `event` to a `Literal` and type `data`, which is what makes the two
    stream unions discriminable. The base is never published on its own.
    """

    event: str

    def to_event(self) -> ServerSentEvent:
        """Wrap this frame for `fastapi.sse`, naming the SSE event from the model itself.

        Taking the name from `self.event` rather than from a string at the call site is the
        whole point: the ``event:`` line and the payload's `event` key are then one fact, so a
        client may switch on either and get the same answer.
        """
        return ServerSentEvent(event=self.event, data=self)
