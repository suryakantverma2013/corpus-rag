"""The durable per-request record, its retention, correlation and the OTel span (T-604, R-79).

Four things are under test here and they fail in different directions, so they are grouped
rather than interleaved:

1. **The durable record** (R-79(1)) — the gap R-43(5) named and deferred. An answered turn
   already had a durable home in `messages`; an **errored** one had none, because R-54(3)
   keeps FR-ERR-04 copy out of that table. The headline test is therefore the error path.
2. **Correlation** (R-79(2)) — `structlog.contextvars`, and the one line that makes it work
   or silently not (`merge_contextvars` in the processor chain).
3. **Retention** (R-79(3)) — including the destructive default, `TELEMETRY_RETENTION_DAYS = 0`
   read as a horizon, which is the one value whose literal reading empties the table.
4. **The span** (R-79(4)) — off by default, and carrying no payload text when on.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models.turn_telemetry import TurnTelemetry
from app.db.repositories.turn_telemetry import TurnTelemetryRepository
from app.logging_config import configure_logging
from app.logging_context import REQUEST_ID_HEADER, bound_turn, new_request_id
from app.rag import telemetry
from app.rag.telemetry import TurnRecord
from app.tracing import TURN_SPAN_NAME, record_turn_span, span_attributes
from workers.retention import prune_turn_telemetry


def make_record(**overrides: object) -> TurnRecord:
    """A closed, answered turn. Overridden per test rather than rebuilt."""
    defaults: dict[str, object] = {
        "conversation_id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "turn_index": 3,
        "outcome": "answered",
        "latency_ms": 1234,
        "model_name": "gpt-4o",
        "prompt_tokens": 900,
        "completion_tokens": 120,
        "message_id": uuid.uuid4(),
        "started_at": 1_770_000_000.5,
        "groundedness": 0.75,
    }
    defaults.update(overrides)
    return TurnRecord(**defaults)  # type: ignore[arg-type]


# --- 1. the durable record ----------------------------------------------------


async def test_a_closed_turn_is_written_with_its_whole_metric_set(session: AsyncSession) -> None:
    """One row, carrying exactly what `graph.turn.end` reports."""
    record = make_record()
    await TurnTelemetryRepository(session).record(record)
    await session.flush()

    row = (
        await session.scalars(
            select(TurnTelemetry).where(TurnTelemetry.conversation_id == record.conversation_id)
        )
    ).one()
    assert row.outcome == "answered"
    assert row.turn_index == 3
    assert row.latency_ms == 1234
    assert row.model_name == "gpt-4o"
    assert row.prompt_tokens == 900
    assert row.completion_tokens == 120
    assert row.message_id == record.message_id
    assert row.owner_id == record.owner_id
    assert row.error_code is None


async def test_an_errored_turn_writes_the_row_that_messages_cannot(session: AsyncSession) -> None:
    """**The gap this table exists for** (R-43(5), closed by R-79(1)).

    R-54(3) serves an errored turn and never stores it: FR-ERR-04 copy in `messages` would be
    charged against the NFR-CAP-01 budget R-51(4) derives from that table. So before T-604 the
    one thing FR-ORC-03 names explicitly — "telemetry logs request **failure**" — had no
    durable subject anywhere, and a user reporting yesterday's broken chat left an operator
    with a log line that had already scrolled away.
    """
    record = make_record(outcome="error", error_code="LLM_ERROR", model_name=None, message_id=None)
    await TurnTelemetryRepository(session).record(record)
    await session.flush()

    row = (
        await session.scalars(
            select(TurnTelemetry).where(TurnTelemetry.conversation_id == record.conversation_id)
        )
    ).one()
    assert row.outcome == "error"
    assert row.error_code == "LLM_ERROR"
    assert row.message_id is None, "an errored turn has no `messages` row to point at"
    assert row.latency_ms == 1234, "and it still reports how long it took to fail"


async def test_the_outcome_column_is_never_null_even_for_a_record_that_lost_one(
    session: AsyncSession,
) -> None:
    """`outcome` is `NOT NULL`, so the repository has to answer for the degenerate case.

    A row that cannot say how the turn ended answers nothing anyone queries this table for.
    The only path reaching here without an outcome is a failure whose class is already known,
    so it is filed as `error`; anything else is `unknown` rather than a lost insert.
    """
    repo = TurnTelemetryRepository(session)
    await repo.record(make_record(outcome=None, error_code="TIMEOUT"))
    await repo.record(make_record(outcome=None, error_code=None, conversation_id=uuid.uuid4()))
    await session.flush()

    outcomes = sorted(row.outcome for row in (await session.scalars(select(TurnTelemetry))).all())
    assert outcomes == ["error", "unknown"]


async def test_a_conversations_history_reads_newest_first(session: AsyncSession) -> None:
    conversation_id = uuid.uuid4()
    repo = TurnTelemetryRepository(session)
    for index in range(3):
        await repo.record(make_record(conversation_id=conversation_id, turn_index=index))
    await repo.record(make_record())  # a different conversation
    await session.flush()

    rows = await repo.list_for_conversation(conversation_id=conversation_id)
    assert len(rows) == 3
    assert {row.conversation_id for row in rows} == {conversation_id}


async def test_counting_discriminates_by_outcome(session: AsyncSession) -> None:
    """The one read an operator actually starts from: how many, and how many failed."""
    repo = TurnTelemetryRepository(session)
    since = datetime.now(UTC) - timedelta(minutes=5)
    await repo.record(make_record())
    await repo.record(make_record(conversation_id=uuid.uuid4(), outcome="error"))
    await repo.record(make_record(conversation_id=uuid.uuid4(), outcome="error"))
    await session.flush()

    assert await repo.count_since(since=since) == 3
    assert await repo.count_since(since=since, outcome="error") == 2


# --- the record's own contract ------------------------------------------------


def test_the_event_kind_is_decided_by_the_record_not_the_caller() -> None:
    """R-43(5) rule 2 made structural.

    `.failure` is reserved for `outcome == "error"`; every other terminal — including an
    abstention, which R-23 makes a *response* — closes as `.end`. Before T-604 that invariant
    lived in an `if` in `finalize` with two independently assembled argument lists, so it was
    true only while the two branches agreed.
    """
    assert make_record(outcome="error").is_error is True
    for outcome in ("answered", "abstained", "blocked", "review", None):
        assert make_record(outcome=outcome).is_error is False, outcome


def test_a_failure_closes_as_failure_and_an_abstention_as_an_end() -> None:
    with structlog.testing.capture_logs() as logs:
        telemetry.turn_closed(make_record(outcome="error", error_code="TIMEOUT"))
        telemetry.turn_closed(make_record(outcome="abstained"))

    assert [entry["event"] for entry in logs] == [telemetry.TURN_FAILURE, telemetry.TURN_END]
    assert logs[0]["error_code"] == "TIMEOUT"
    assert logs[1]["outcome"] == "abstained"


def test_the_record_has_no_field_that_could_carry_payload_text() -> None:
    """R-43(5)'s "no payload text, ever", enforced on the type rather than on call sites.

    Every sink — the log event, the durable row, the OTel span — is built from this one
    object, so a text field added here would leak into all three at once. The guard is the
    field *set*, not a review habit: ids, an outcome, a failure class, four numbers and a
    clock reading, and nothing whose name or type admits a query, an answer or a passage.
    """
    permitted = {
        "conversation_id",
        "owner_id",
        "turn_index",
        "outcome",
        "latency_ms",
        "error_code",
        "model_name",
        "prompt_tokens",
        "completion_tokens",
        "message_id",
        "started_at",
        # T-609/R-80(1): the gate's own coverage score. Added deliberately, and this guard is
        # what forced the decision to be made rather than drifted into — a float, an operator
        # store only, and R-49(1)'s prohibition on `messages.evaluation` untouched.
        "groundedness",
    }
    assert {field.name for field in fields(TurnRecord)} == permitted, (
        "a field was added to TurnRecord — check it cannot carry payload text (R-43(5)), "
        "then add it here and to `app.tracing.span_attributes`"
    )


# --- 2. correlation -----------------------------------------------------------


def test_merge_contextvars_is_configured_and_is_first() -> None:
    """The line the whole correlation feature depends on, and it fails **silently**.

    Without `merge_contextvars` in the chain the bindings are set and never rendered: every
    call site looks correct, every test that asserts an explicit key still passes, and the
    ids are simply absent from the output. First in the chain so the bound values are present
    for every processor after it.
    """
    configure_logging()
    processors = structlog.get_config()["processors"]
    assert processors[0] is structlog.contextvars.merge_contextvars


def test_the_turn_binding_reaches_an_event_that_never_mentions_the_turn() -> None:
    """The actual point: an emitter that knows nothing about the turn is still correlatable.

    `rag.router.*`, `rag.rerank.*`, `security.injection.*` and `graph.node_failed` each named
    their own subject and nothing else, so a slow router call could not be tied to the turn it
    belonged to. This asserts the mechanism against a logger that passes no ids at all — which
    is what makes it hold for emitters written after this test.
    """
    conversation_id = uuid.uuid4()
    unrelated = structlog.get_logger("app.rag.router")

    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        with bound_turn(conversation_id=conversation_id, turn_index=7):
            unrelated.info("rag.router.classified", query_class="simple")
        unrelated.info("rag.router.classified", query_class="simple")

    inside, outside = logs
    assert inside["conversation_id"] == str(conversation_id)
    assert inside["turn_index"] == 7
    assert "conversation_id" not in outside, "the binding outlived its turn"


def test_a_nested_turn_restores_rather_than_clears_its_parents_binding() -> None:
    """Regenerate runs a second turn inside one request; the outer ids must survive it."""
    outer = uuid.uuid4()
    inner = uuid.uuid4()
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        log = structlog.get_logger("test")
        with bound_turn(conversation_id=outer, turn_index=1):
            with bound_turn(conversation_id=inner, turn_index=2):
                log.info("inner")
            log.info("back.outside")

    assert logs[0]["conversation_id"] == str(inner)
    assert logs[1]["conversation_id"] == str(outer), "the nested scope erased its parent"
    assert logs[1]["turn_index"] == 1


async def test_every_response_carries_a_correlation_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


async def test_a_supplied_correlation_id_survives_the_hop(client: httpx.AsyncClient) -> None:
    """Honouring an inbound id is the whole point of the header — a proxy's id must win."""
    supplied = new_request_id()
    response = await client.get("/health", headers={REQUEST_ID_HEADER: supplied})
    assert response.headers[REQUEST_ID_HEADER] == supplied


@pytest.mark.parametrize(
    ("hostile", "why"),
    [
        ("a" * 500, "unbounded length is a log-flooding primitive"),
        ("id\nlevel=critical", "a newline in a log line is log injection"),
        ("id with spaces", "not the shape of an id"),
        ("", "empty is not an id"),
        ("   ", "whitespace is not an id"),
    ],
)
async def test_a_hostile_correlation_id_is_replaced_not_bound(
    client: httpx.AsyncClient, hostile: str, why: str
) -> None:
    """The header is attacker-controlled and it ends up in a log line."""
    response = await client.get("/health", headers={REQUEST_ID_HEADER: hostile})
    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != hostile, why
    assert len(echoed) == 32 and echoed.isalnum()


# --- 3. retention -------------------------------------------------------------


async def test_retention_removes_what_is_past_the_horizon_and_nothing_else(
    session: AsyncSession,
) -> None:
    repo = TurnTelemetryRepository(session)
    fresh = await repo.record(make_record())
    stale = await repo.record(make_record(conversation_id=uuid.uuid4()))
    await session.flush()
    # `created_at` is a server default, so age is applied after the insert.
    await session.execute(
        text("UPDATE turn_telemetry SET created_at = now() - interval '100 days' WHERE id = :id"),
        {"id": stale.id},
    )

    removed = await repo.prune(older_than_days=90, batch=1000)
    await session.flush()

    assert removed == 1
    survivors = [row.id for row in (await session.scalars(select(TurnTelemetry))).all()]
    assert survivors == [fresh.id]


async def test_retention_is_batch_bounded_and_converges(session: AsyncSession) -> None:
    """One pass may not lock a large slice of the table (R-65's rule, R-79(3) inherits it)."""
    repo = TurnTelemetryRepository(session)
    for _ in range(5):
        await repo.record(make_record(conversation_id=uuid.uuid4()))
    await session.flush()
    await session.execute(text("UPDATE turn_telemetry SET created_at = now() - interval '99 days'"))

    assert await repo.prune(older_than_days=90, batch=2) == 2
    assert await repo.prune(older_than_days=90, batch=2) == 2
    assert await repo.prune(older_than_days=90, batch=2) == 1
    assert await repo.prune(older_than_days=90, batch=2) == 0


async def test_a_zero_horizon_keeps_everything_rather_than_deleting_it(
    db_connection: AsyncConnection, session: AsyncSession
) -> None:
    """`TELEMETRY_RETENTION_DAYS = 0` means **keep forever**, and it has to.

    Read literally as a horizon, 0 is "delete everything older than now" — the single value
    whose plain reading empties the table. The polarity is therefore the opposite of
    `CHECKPOINTER_RETENTION_INTERVAL_SECONDS`, where 0 disables by meaning "never run", and
    the guard is deliberately in both the cron registration and the task.
    """
    repo = TurnTelemetryRepository(session)
    await repo.record(make_record())
    await session.flush()
    await session.execute(
        text("UPDATE turn_telemetry SET created_at = now() - interval '999 days'")
    )
    await session.commit()

    settings = Settings()
    settings.telemetry.retention_days = 0
    sessions = async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )

    assert await prune_turn_telemetry({"settings": settings, "sessionmaker": sessions}) == 0
    assert len((await session.scalars(select(TurnTelemetry))).all()) == 1


async def test_the_retention_task_never_raises(db_connection: AsyncConnection) -> None:
    """Retention is maintenance: a failed pass costs disk until the next one.

    An exception marks the cron job failed and puts an incident in front of an operator that
    no user can see — the R-50 disposition, where the degraded output is "nothing pruned".
    """

    class _Broken:
        def __call__(self) -> None:
            raise RuntimeError("database gone")

    settings = Settings()
    settings.telemetry.retention_days = 90
    assert await prune_turn_telemetry({"settings": settings, "sessionmaker": _Broken()}) == 0


def test_retention_defaults_are_the_ruled_ones() -> None:
    """NFR-OBS-04 settled at 90 days by R-79(3); a change here is a spec change."""
    telemetry_settings = Settings().telemetry
    assert telemetry_settings.retention_days == 90
    assert telemetry_settings.retention_interval_seconds == 86_400.0


# --- 4. the OTel span ---------------------------------------------------------


def test_tracing_is_off_by_default() -> None:
    """§10.4 lists the observability row as an unadopted scale-out option (R-79(4)).

    `opentelemetry-sdk` is present transitively, so an operator running
    `opentelemetry-instrument` would otherwise start exporting turn attributes without ever
    having asked for it.
    """
    assert Settings().telemetry.tracing_enabled is False


def test_a_disabled_exporter_creates_no_span(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    class _Tracer:
        def start_span(self, name: str, **_: object) -> object:
            started.append(name)
            raise AssertionError("a span was started with tracing disabled")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: _Tracer())
    settings = Settings()
    settings.telemetry.tracing_enabled = False

    record_turn_span(make_record(), settings=settings)
    assert started == []


def test_an_enabled_exporter_emits_one_span_with_the_real_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The span's duration is *computed*, never read from a second clock.

    A span stamped "now" at both ends reports a zero-duration turn; one whose end is read
    fresh reports a different number from `latency_ms`, and NFR-OBS-02's identity is exactly
    the property that would break.
    """
    captured: dict[str, object] = {}

    class _Span:
        def set_status(self, status: object) -> None:
            captured["status"] = status

        def end(self, end_time: int | None = None) -> None:
            captured["end_time"] = end_time

    class _Tracer:
        def start_span(self, name: str, **kwargs: object) -> _Span:
            captured["name"] = name
            captured.update(kwargs)
            return _Span()

    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: _Tracer())
    settings = Settings()
    settings.telemetry.tracing_enabled = True

    record = make_record(started_at=1_770_000_000.0, latency_ms=1500)
    record_turn_span(record, settings=settings)

    assert captured["name"] == TURN_SPAN_NAME
    assert captured["start_time"] == 1_770_000_000 * 1_000_000_000
    assert captured["end_time"] == captured["start_time"] + 1_500 * 1_000_000
    assert "status" not in captured, "an answered turn is not an error"


def test_a_failed_turns_span_carries_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Span:
        def set_status(self, status: object) -> None:
            captured["status"] = status

        def end(self, end_time: int | None = None) -> None:  # noqa: ARG002
            return None

    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer",
        lambda _name: type("_T", (), {"start_span": lambda _self, _n, **_k: _Span()})(),
    )
    settings = Settings()
    settings.telemetry.tracing_enabled = True

    record_turn_span(make_record(outcome="error", error_code="RATE_LIMITED"), settings=settings)
    assert captured["status"] is not None


def test_the_span_exports_no_payload_text() -> None:
    """Every attribute traces back to a `TurnRecord` field, which cannot hold text.

    Asserted as a *derivation* rather than as a literal list: an attribute added here that
    does not correspond to a record field fails, which is the direction a leak would arrive
    from (an exporter is the one sink that sends data off the machine).
    """
    record = make_record()
    attributes = span_attributes(record)
    record_fields = {field.name for field in fields(TurnRecord)}
    for key in attributes:
        assert key.startswith("corpus.")
        assert key.removeprefix("corpus.") in record_fields, key
    assert "started_at" not in {key.removeprefix("corpus.") for key in attributes}, (
        "the start instant is the span's own, not an attribute"
    )


def test_an_absent_metric_is_omitted_rather_than_exported_as_empty() -> None:
    """OTel attributes are typed and have no null.

    `""` for `model_name` would read as "a model with no name" where the truth is that the
    turn never reached generation.
    """
    attributes = span_attributes(
        make_record(model_name=None, prompt_tokens=None, completion_tokens=None, message_id=None)
    )
    assert "corpus.model_name" not in attributes
    assert "corpus.prompt_tokens" not in attributes
    assert "corpus.message_id" not in attributes
    assert attributes["corpus.latency_ms"] == 1234


def test_the_span_never_takes_the_turn_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is called from `finalize`, which must never raise (R-42(5))."""

    def _explode(_name: str) -> object:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)
    settings = Settings()
    settings.telemetry.tracing_enabled = True

    record_turn_span(make_record(), settings=settings)  # must not raise


async def test_the_gate_score_travels_from_state_into_the_row(
    db_connection: AsyncConnection, session: AsyncSession
) -> None:
    """T-609/R-80(1): `RAGState.groundedness` must actually reach `turn_telemetry`.

    **This test exists because the first one written for it was vacuous.** The route-level
    assertion in `test_chat_api.py` drives an empty-scope abstention, which never reaches the
    gate and so writes `groundedness = None` — meaning a mutation that hard-coded `None`
    passed the whole suite. That is §8.65(5) exactly: the test named the wiring and never
    reached it. Driving `finalize` with a scored turn is the only way to fail on the defect.

    An `error` outcome so `_should_persist` is False: no `messages` row is needed, and what is
    under test is the state → record → row path and nothing else.
    """
    from langgraph.runtime import Runtime

    from app.rag.graph import finalize
    from app.rag.state import RAGContext, fresh_turn_state

    conversation_id = uuid.uuid4()
    sessions = async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    context = RAGContext(
        owner_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        conversation_id=conversation_id,
        sessionmaker=sessions,
    )
    state = {
        **fresh_turn_state(),
        "outcome": "error",
        "error_code": "LLM_ERROR",
        # `time.time()`, not a fixed epoch: `latency_ms` is `now - started_at`, so a constant
        # from the past grows without bound and eventually overflows the int32 column — which
        # is exactly how this test failed when first written.
        "started_at": time.time() - 0.05,
        "turn_index": 0,
        "groundedness": 0.375,
    }

    await finalize(state, Runtime(context=context))  # type: ignore[arg-type]

    row = (
        await session.scalars(
            select(TurnTelemetry).where(TurnTelemetry.conversation_id == conversation_id)
        )
    ).one()
    assert row.groundedness == pytest.approx(0.375), (
        "the gate's score must reach the operator store, or GATE_MIN_GROUNDEDNESS stays "
        "uncalibratable however much feedback accumulates"
    )
    assert row.outcome == "error"
