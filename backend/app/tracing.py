"""OpenTelemetry span emission for a closed turn (NFR-OBS-05, T-604, R-79(4)).

NFR-OBS-05 is *optional* — "LLM and retrieval traces, cost, latency and evaluation feedback
**may** be exported to LangSmith and/or Langfuse, and service traces via OpenTelemetry" —
and §10.4 lists the whole row as an unadopted scale-out option whose trigger ("external
trace/eval/cost dashboards needed") is not met by any environment this product runs in.
R-79(4) therefore ships the one emitter that costs nothing to leave off, and declines the
two vendor SDKs:

* **LangSmith and Langfuse are declined with a trigger.** Both work by *wrapping* the model
  client (`wrap_openai`, `@observe`), which would put a vendor inside the R-15/R-45(9)
  `ChatClient` seam and inside the DeepEval judge path R-50(2) kept behind it — and the
  value both sell is capturing prompt and completion **text**, which R-43(5)'s "no payload
  text, ever" forbids and which NFR-SEC-03 (an open `(D)` on data at rest and in transit)
  has not settled. Adopting either is a data-egress decision, not an instrumentation one.
  **They also both ingest OTLP**, so one vendor-neutral emitter reaches all three names in
  NFR-OBS-05 without three SDKs. Revisit when an external dashboard is actually deployed
  *and* NFR-SEC-03 is settled.
* **What ships is API-only.** `opentelemetry-api` is a declared dependency (it was already
  in the graph transitively via `deepeval`; promoted on R-42(8)'s `psycopg-pool` precedent,
  because a package we import must be one we asked for). The **SDK, sampler, exporter and
  collector are a deployment concern**, configured through the standard `OTEL_*`
  environment. Without a tracer provider the OTel API is a documented no-op, so the
  disabled path costs one attribute lookup.

**One span per turn, at the end, and that is a design consequence rather than a shortcut.**
A real parent span held open across `route → retrieve → rerank → generate` would have to
survive a superstep boundary, and a checkpointed run can span a **process restart** by
construction (R-43(8) is the same argument for using wall-clock rather than
`time.monotonic()`) — there is nothing to hold the span object in. So the turn is emitted
as a single span whose start and end are *computed* from `started_at` and `latency_ms`,
giving it the true duration without pretending a live context existed. Node-level child
spans would need the span context carried in `RAGState`, which R-42(2) keeps to ids and
scalars; that is the shape a future ruling would have to take, not an oversight here.

**No payload text.** The attributes are exactly :class:`~app.rag.telemetry.TurnRecord`'s
fields, which have no member that could carry a query or an answer. A test asserts the
attribute set against that dataclass, so adding a text field to the record would fail here
rather than silently start exporting it.
"""

from __future__ import annotations

import structlog

from app.config import Settings, get_settings
from app.rag.telemetry import TurnRecord

__all__ = ["TURN_SPAN_NAME", "span_attributes", "record_turn_span"]

log = structlog.get_logger(__name__)

#: The span name. `graph.turn` rather than `graph.turn.end`, because a span *is* the
#: interval — naming it after its closing event would read as an instant.
TURN_SPAN_NAME = "graph.turn"

_NANOS_PER_SECOND = 1_000_000_000
_NANOS_PER_MILLISECOND = 1_000_000


def span_attributes(record: TurnRecord) -> dict[str, str | int]:
    """The R-43(5) key set as OTel attributes, with absent values omitted.

    Omitted rather than exported as an empty string: OTel attributes are typed and have no
    null, and `""` for `model_name` would read as "a model with no name" where the truth is
    that the turn never reached generation. `owner_id` is included because a telemetry
    backend's tenancy filter needs it, and it is an opaque id — not a name or an address.
    """
    attributes: dict[str, str | int] = {
        "corpus.conversation_id": str(record.conversation_id),
        "corpus.owner_id": str(record.owner_id),
        "corpus.latency_ms": record.latency_ms,
    }
    if record.turn_index is not None:
        attributes["corpus.turn_index"] = record.turn_index
    if record.outcome is not None:
        attributes["corpus.outcome"] = record.outcome
    if record.error_code is not None:
        attributes["corpus.error_code"] = record.error_code
    if record.model_name is not None:
        attributes["corpus.model_name"] = record.model_name
    if record.prompt_tokens is not None:
        attributes["corpus.prompt_tokens"] = record.prompt_tokens
    if record.completion_tokens is not None:
        attributes["corpus.completion_tokens"] = record.completion_tokens
    if record.message_id is not None:
        attributes["corpus.message_id"] = str(record.message_id)
    return attributes


def record_turn_span(record: TurnRecord, *, settings: Settings | None = None) -> None:
    """Emit one span for a closed turn. A no-op unless `TELEMETRY_TRACING_ENABLED`.

    **Never raises.** It is called from `finalize`, the one node R-42(5) makes structurally
    unskippable and which must never raise (its own error handler routes back to it, so an
    exception here would loop to the recursion limit). An observability exporter is the last
    thing that may take a turn down — the same disposition as the R-50 evaluation path,
    where the degraded output is simply "no trace".

    The enable flag is ours rather than left to "is an SDK installed", because
    `opentelemetry-sdk` *is* installed transitively and an operator running
    `opentelemetry-instrument` would otherwise start exporting these spans without having
    asked. Defaulting off keeps §10.4's "unadopted" honest, and makes turning it on a
    reviewable one-line change.
    """
    settings = settings or get_settings()
    if not settings.telemetry.tracing_enabled:
        return

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(__name__)
        start_ns = (
            int(record.started_at * _NANOS_PER_SECOND) if record.started_at is not None else None
        )
        span = tracer.start_span(
            TURN_SPAN_NAME, start_time=start_ns, attributes=span_attributes(record)
        )
        if record.is_error:
            span.set_status(trace.Status(trace.StatusCode.ERROR, record.error_code or "error"))
        end_ns = (
            start_ns + record.latency_ms * _NANOS_PER_MILLISECOND if start_ns is not None else None
        )
        span.end(end_time=end_ns)
    except Exception:  # pragma: no cover - defensive; see the never-raises note above
        log.warning("telemetry.span_failed", exc_info=True)
