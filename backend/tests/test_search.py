"""Turn-level retrieval: probe fan-out, merge, degradation (T-305, R-46). No database.

`app.rag.search` is the layer between the graph node and `HybridRetriever`, and everything
it decides — which probes are searched, what happens when one fails, how the lists combine,
how many candidates survive — is decided without touching Postgres. So it is tested without
Postgres, on the split `tests/test_fusion.py` and `tests/test_retrieval.py` already draw:
the arithmetic and the policy here, the SQL there.

The retriever double records the query text and the session it was built with, because two
of this module's claims are otherwise invisible: that the **original query is always one of
the probes** (R-45(3)) and that each probe gets **its own session** (an `AsyncSession` is
not safe for concurrent statements, so sharing one across a gather is a real bug that no
assertion on the results would catch).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
import structlog

from app.config import RerankSettings, RetrievalSettings, RouterSettings, Settings
from app.rag.retrieval import RetrievalFilter, RetrievedChunk
from app.rag.search import PROBE_FAILED, RETRIEVAL_COMPLETED, build_probes, retrieve_for_turn
from app.services.embeddings import FakeEmbeddingClient

OWNER_ID = uuid.uuid4()
FILTERS = RetrievalFilter(owner_id=OWNER_ID, conversation_id=uuid.uuid4())


def _settings(**overrides: object) -> Settings:
    """Real `Settings` with only the two groups this module reads overridden."""
    retrieval = RetrievalSettings(**overrides.pop("retrieval", {}))  # type: ignore[arg-type]
    router = RouterSettings(**overrides.pop("router", {}))  # type: ignore[arg-type]
    rerank = RerankSettings(**overrides.pop("rerank", {}))  # type: ignore[arg-type]
    return Settings(retrieval=retrieval, router=router, rerank=rerank)  # type: ignore[arg-type]


def _chunk(seed: int, **overrides: object) -> RetrievedChunk:
    fields: dict[str, object] = {
        "chunk_id": uuid.UUID(int=seed),
        "document_id": uuid.UUID(int=1000),
        "knowledge_base_id": uuid.UUID(int=2000),
        "filename": "handbook.pdf",
        "chunk_index": seed,
        "chunk_text": f"passage {seed}",
        "score": 1.0,
    }
    fields.update(overrides)
    return RetrievedChunk(**fields)  # type: ignore[arg-type]


class _StubSession:
    """A session that can do nothing except be opened and closed, and say who opened it."""

    opened = 0

    def __init__(self) -> None:
        type(self).opened += 1
        self.serial = type(self).opened
        self.closed = False

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.closed = True
        return False


class _StubRetriever:
    """Answers each probe from a script keyed on the probe text; records what it was asked."""

    def __init__(
        self,
        results: dict[str, list[RetrievedChunk]] | None = None,
        *,
        default: list[RetrievedChunk] | None = None,
        errors: dict[str, Exception] | None = None,
        record: list[tuple[str, object]] | None = None,
        session: object = None,
    ) -> None:
        self.results = results or {}
        self.default = default if default is not None else []
        self.errors = errors or {}
        self.record = record if record is not None else []
        self.session = session

    async def search(
        self,
        query_text: str,
        query_embedding: Sequence[float],  # noqa: ARG002
        *,
        filters: RetrievalFilter,  # noqa: ARG002
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        self.record.append((query_text, self.session, top_k))
        if query_text in self.errors:
            raise self.errors[query_text]
        return list(self.results.get(query_text, self.default))


def _factory(**kwargs: object):  # noqa: ANN202
    """A retriever factory that hands each probe's session to its own retriever."""
    record: list[tuple[str, object]] = []

    def build(session: object) -> _StubRetriever:
        return _StubRetriever(record=record, session=session, **kwargs)  # type: ignore[arg-type]

    build.record = record  # type: ignore[attr-defined]
    return build


async def _run(
    query: str = "what is the escalation policy?",
    sub_queries: Sequence[str] = (),
    *,
    factory=None,  # noqa: ANN001
    embeddings: object = None,
    settings: Settings | None = None,
    filters: RetrievalFilter = FILTERS,
) -> list[RetrievedChunk]:
    return await retrieve_for_turn(
        query=query,
        sub_queries=sub_queries,
        filters=filters,
        sessionmaker=_StubSession,
        embeddings=embeddings or FakeEmbeddingClient(),  # type: ignore[arg-type]
        retriever_factory=factory or _factory(),
        settings=settings or _settings(),
    )


# --- the probe list -----------------------------------------------------------


def test_the_query_is_always_the_first_probe() -> None:
    """R-45(3): probes are *added to* the query, never a replacement for it."""
    probes = build_probes("  original  ", ["rewritten"], settings=_settings())
    assert probes == ["original", "rewritten"]


def test_no_sub_queries_means_exactly_one_probe() -> None:
    assert build_probes("original", [], settings=_settings()) == ["original"]


def test_a_probe_equal_to_the_query_is_dropped() -> None:
    """It would return the identical list and double that list's weight in the merge."""
    assert build_probes("original", ["original", " original "], settings=_settings()) == [
        "original"
    ]


def test_duplicate_probes_are_dropped() -> None:
    probes = build_probes("q", ["a", "a", "b"], settings=_settings())
    assert probes == ["q", "a", "b"]


def test_blank_and_non_string_probes_are_dropped() -> None:
    probes = build_probes("q", ["", "   ", None, 7, "real"], settings=_settings())  # type: ignore[list-item]
    assert probes == ["q", "real"]


def test_probes_from_an_older_checkpoint_are_recapped() -> None:
    """The reason this re-caps rather than trusting the router (R-46(5)).

    `sub_queries` comes back from a checkpoint that may have been written when
    `ROUTER_MAX_SUB_QUERIES` was larger. A resumed turn must cost what *this* deployment
    says it costs, so the cap is applied on read, not only on write.
    """
    settings = _settings(router={"max_sub_queries": 2})
    probes = build_probes("q", ["a", "b", "c", "d"], settings=settings)
    assert probes == ["q", "a", "b"]


def test_an_over_long_probe_is_truncated_not_discarded() -> None:
    """Dropping it would silently downgrade `hyde` to plain hybrid (R-45(3))."""
    settings = _settings(router={"max_probe_chars": 10})
    probes = build_probes("q", ["x" * 400], settings=settings)
    assert probes == ["q", "x" * 10]


def test_zero_max_sub_queries_still_searches_the_query() -> None:
    settings = _settings(router={"max_sub_queries": 0})
    assert build_probes("q", ["a"], settings=settings) == ["q"]


# --- fan-out ------------------------------------------------------------------


async def test_every_probe_is_searched_and_each_gets_its_own_session() -> None:
    """The concurrency invariant: one `AsyncSession` per probe, never one shared."""
    factory = _factory(default=[_chunk(1)])
    await _run("q", ["a", "b"], factory=factory)

    asked = [probe for probe, _session, _top_k in factory.record]  # type: ignore[attr-defined]
    sessions = {id(session) for _probe, session, _top_k in factory.record}  # type: ignore[attr-defined]
    assert asked == ["q", "a", "b"]
    assert len(sessions) == 3


async def test_each_probe_fetches_the_per_probe_fusion_top_k() -> None:
    """R-46(4): `RETRIEVAL_FUSION_TOP_K` stays per probe; the merged set has its own cap."""
    factory = _factory(default=[_chunk(1)])
    await _run("q", ["a"], factory=factory, settings=_settings(retrieval={"fusion_top_k": 7}))

    assert [top_k for _probe, _session, top_k in factory.record] == [7, 7]  # type: ignore[attr-defined]


async def test_the_probes_are_embedded_in_one_round_trip() -> None:
    """R-46(7): one request on the query budget, not one per probe."""
    embeddings = FakeEmbeddingClient()
    await _run("q", ["a", "b"], embeddings=embeddings)

    assert embeddings.request_count == 1
    assert embeddings.embedded_inputs == 3


async def test_probes_are_searched_concurrently() -> None:
    """`asyncio.gather`, not a loop — three 50 ms probes must not cost 150 ms.

    Asserted with a barrier rather than a stopwatch: each probe waits for all three to
    arrive, so a sequential implementation deadlocks into the timeout instead of merely
    being slow, and there is no clock to make the test flaky on a loaded machine.
    """
    barrier = asyncio.Barrier(3)

    class _Barrier(_StubRetriever):
        async def search(self, query_text, query_embedding, *, filters, top_k=None):  # noqa: ANN001, ANN201
            async with asyncio.timeout(5):
                await barrier.wait()
            return await super().search(query_text, query_embedding, filters=filters, top_k=top_k)

    def factory(session: object) -> _Barrier:  # noqa: ARG001
        return _Barrier(default=[_chunk(1)])

    hits = await _run("q", ["a", "b"], factory=factory)
    assert hits


# --- degradation --------------------------------------------------------------


async def test_a_failing_derived_probe_degrades_the_turn_but_does_not_fail_it() -> None:
    """R-46(6): probes are additive, so losing one costs recall, not correctness."""
    factory = _factory(
        results={"q": [_chunk(1)], "b": [_chunk(2)]},
        errors={"a": RuntimeError("probe exploded")},
    )
    with structlog.testing.capture_logs() as logs:
        hits = await _run("q", ["a", "b"], factory=factory)

    assert {hit.chunk_id for hit in hits} == {uuid.UUID(int=1), uuid.UUID(int=2)}
    failures = [entry for entry in logs if entry["event"] == PROBE_FAILED]
    assert len(failures) == 1
    assert failures[0]["probe_index"] == 1


async def test_the_original_querys_failure_fails_the_turn() -> None:
    """The other half of R-46(6), and the reason `retrieve` has no `try`.

    FR-RET-05 is explicit that an unavailable store returns a graceful error rather than an
    answer. Swallowing this one would ground the turn in whatever a *rewrite* happened to
    find — an answer to a question the user did not ask, presented as if it were grounded.
    """
    factory = _factory(results={"a": [_chunk(2)]}, errors={"q": RuntimeError("the store is down")})
    with pytest.raises(RuntimeError, match="store is down"):
        await _run("q", ["a"], factory=factory)


async def test_a_failure_log_carries_no_probe_text() -> None:
    """R-43(5)/R-45(8) applied here: counts and codes, never the payload."""
    factory = _factory(errors={"secret probe text": RuntimeError("boom")})
    with structlog.testing.capture_logs() as logs:
        await _run("q", ["secret probe text"], factory=factory)

    rendered = repr(logs)
    assert "secret probe text" not in rendered
    assert "RuntimeError" in rendered


async def test_an_empty_query_retrieves_nothing_without_calling_out() -> None:
    embeddings = FakeEmbeddingClient()
    factory = _factory(default=[_chunk(1)])

    assert await _run("   ", factory=factory, embeddings=embeddings) == []
    assert embeddings.request_count == 0
    assert factory.record == []  # type: ignore[attr-defined]


# --- merge --------------------------------------------------------------------


async def test_a_chunk_several_probes_agree_on_outranks_one_probes_top_hit() -> None:
    """The R-46(3) property, end to end through the node's own assembly."""
    agreed, single = _chunk(1), _chunk(2)
    factory = _factory(
        results={
            "q": [single, agreed],
            "a": [agreed],
            "b": [agreed],
        }
    )
    hits = await _run("q", ["a", "b"], factory=factory)

    assert [hit.chunk_id for hit in hits] == [agreed.chunk_id, single.chunk_id]


async def test_the_merged_score_replaces_the_per_probe_score() -> None:
    """Downstream reads one number; it must mean the merge, not probe 1's fusion."""
    hits = await _run("q", ["a"], factory=_factory(default=[_chunk(1, score=0.5)]))

    assert hits[0].score == pytest.approx(1 / 61 + 1 / 61)


async def test_the_representative_hit_comes_from_the_original_query() -> None:
    """First writer wins, and the query is probe 0 — so the diagnostics stay attached to it."""
    from_query = _chunk(1, dense_rank=1, sparse_rank=None)
    from_probe = _chunk(1, dense_rank=9, sparse_rank=9)
    factory = _factory(results={"q": [from_query], "a": [from_probe]})

    hits = await _run("q", ["a"], factory=factory)
    assert hits[0].dense_rank == 1
    assert hits[0].sparse_rank is None


async def test_the_merged_set_is_truncated_to_the_merged_cap() -> None:
    factory = _factory(default=[_chunk(seed) for seed in range(1, 11)])
    # The FR-RET-02 top-K comes down with it: R-47(3) refuses a top-K above the merge
    # ceiling, because a reranker asked for more passages than retrieval can produce is a
    # knob that silently does nothing.
    settings = _settings(retrieval={"merged_top_k": 4}, rerank={"top_k": 4})
    hits = await _run("q", ["a"], factory=factory, settings=settings)

    assert len(hits) == 4


async def test_one_probe_returns_what_the_retriever_returned_in_order() -> None:
    """The single-probe identity at this level: no reordering, no truncation at 50 > 30."""
    ordered = [_chunk(seed) for seed in range(1, 6)]
    hits = await _run("q", factory=_factory(default=ordered))

    assert [hit.chunk_id for hit in hits] == [chunk.chunk_id for chunk in ordered]


async def test_overlapping_neighbours_are_dropped_after_the_merge() -> None:
    """R-37(6): the survivor is whichever ranks higher *after* fusion, not before it."""
    meta = {"block_order": 0, "block_chunk_index": 1}
    neighbour = {"block_order": 0, "block_chunk_index": 2}
    factory = _factory(
        default=[_chunk(1, meta=meta), _chunk(2, meta=neighbour)],
    )
    hits = await _run("q", factory=factory)

    assert [hit.chunk_id for hit in hits] == [uuid.UUID(int=1)]


async def test_the_completion_log_counts_but_never_quotes() -> None:
    factory = _factory(default=[_chunk(1)])
    with structlog.testing.capture_logs() as logs:
        await _run("a very distinctive question", ["a distinctive probe"], factory=factory)

    completed = [entry for entry in logs if entry["event"] == RETRIEVAL_COMPLETED]
    assert len(completed) == 1
    assert completed[0]["probes"] == 2
    assert completed[0]["returned"] == 1
    assert "distinctive" not in repr(completed)


# --- module hygiene -----------------------------------------------------------


def test_search_module_imports_no_langgraph() -> None:
    """Same guard as `app.rag.router`/`app.rag.errors`, for the same reason.

    Importing this module must not drag in langgraph's serializer flag machinery — T-402's
    chat route and T-604's telemetry both want retrieval's shape without it.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.rag.search, sys;"
            "leaked=[m for m in sys.modules if m.startswith(('langgraph','langchain'))];"
            "print(leaked)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"
