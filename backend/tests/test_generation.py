"""FR-SYS-02 generation: prompt hand-off, `[S<n>]` segmentation, fail-closed (T-307, R-48).

No database and no model, on the split `tests/test_rerank.py` and `tests/test_search.py` draw.
`app.rag.generation` decides two things that matter and neither needs Postgres: what the model
is shown (which is R-44(3)'s payload, unchanged and *not* re-composed here) and how its prose
becomes the FR-MSG-06 segments a citation chip is rendered from.

The segmentation tests carry most of the weight. A parser that silently mis-resolves a marker
produces an answer that reads perfectly and cites the wrong passage — the failure R-44(7)
warns about, and the one a human reviewer is least likely to catch.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import LlmSettings, Settings
from app.rag import generation as generation_module
from app.rag.generation import (
    AnswerSegment,
    cited_chunk_ids,
    compose_answer_prompt,
    generate_answer,
    split_answer_segments,
)
from app.rag.prompts import SYSTEM_PROMPT, PromptSource
from app.security.prompt_injection import (
    CONTEXT_FENCE_CLOSE,
    CONTEXT_FENCE_OPEN,
    source_marker,
)
from app.services.llm import (
    ChatRefusedError,
    ChatResponseError,
    ChatUnavailableError,
    FakeChatClient,
)

QUERY = "What is the refund window for damaged goods?"

IDS = [str(uuid.UUID(int=index)) for index in range(1, 4)]


def _sources(count: int = 3) -> list[PromptSource]:
    return [
        PromptSource(
            chunk_id=IDS[index - 1],
            filename=f"policy{index}.pdf",
            text=f"Refunds are accepted within {index * 10} days.",
            locator=f"p. {index}",
        )
        for index in range(1, count + 1)
    ]


def _settings(**llm: object) -> Settings:
    return Settings(llm=LlmSettings(**llm))  # type: ignore[arg-type]


# --- segmentation: the resolvable cases ---------------------------------------


def test_prose_with_no_markers_is_one_text_segment() -> None:
    segments, dropped = split_answer_segments("Refunds take 30 days.", IDS)
    assert segments == [AnswerSegment(text="Refunds take 30 days.")]
    assert dropped == 0


def test_a_marker_becomes_a_citation_segment_carrying_its_chunk_id() -> None:
    """R-48(5): `[S<k>]` resolves to `source_ids[k-1]` and to nothing else."""
    segments, dropped = split_answer_segments("Refunds take 30 days [S2].", IDS)
    assert segments == [
        AnswerSegment(text="Refunds take 30 days "),
        AnswerSegment(text="[S2]", chunk_id=IDS[1]),
        AnswerSegment(text="."),
    ]
    assert dropped == 0


def test_the_segments_reconstruct_the_answer_verbatim() -> None:
    """A citation segment keeps its own marker text, so nothing is lost in the split.

    That is what lets T-308 reject a citation without re-splitting, and T-402 render either
    a chip or the literal text depending on what survives FR-CIT-06.
    """
    answer = "[S1] Refunds take 30 days [S2], or 20 [S3] for damaged goods."
    segments, _ = split_answer_segments(answer, IDS)
    assert "".join(segment.text for segment in segments) == answer


def test_adjacent_markers_are_two_citations_not_one() -> None:
    segments, _ = split_answer_segments("Both agree [S1][S2].", IDS)
    assert [segment.chunk_id for segment in segments] == [None, IDS[0], IDS[1], None]


def test_a_marker_may_open_or_close_the_answer() -> None:
    leading, _ = split_answer_segments("[S1] says so.", IDS)
    assert leading[0] == AnswerSegment(text="[S1]", chunk_id=IDS[0])
    trailing, _ = split_answer_segments("It says so. [S3]", IDS)
    assert trailing[-1] == AnswerSegment(text="[S3]", chunk_id=IDS[2])


def test_a_repeated_marker_resolves_every_time() -> None:
    """Not deduplicated at the segment level: the same passage can support two claims, and
    each one needs its own chip beside the sentence it supports."""
    segments, _ = split_answer_segments("First [S1]. Second [S1].", IDS)
    assert [segment.chunk_id for segment in segments].count(IDS[0]) == 2


def test_markers_survive_around_markdown_and_unicode() -> None:
    """FR-MSG-07 renders the body as Markdown, so the parser must not care about it."""
    answer = "**Refunds** take 30 days [S1] — see the *policy* “附录” [S2].\n\n- item [S3]"
    segments, dropped = split_answer_segments(answer, IDS)
    assert dropped == 0
    assert cited_chunk_ids(segments) == IDS
    assert "".join(segment.text for segment in segments) == answer


# --- segmentation: the unresolvable cases (R-48(6)) ---------------------------


def test_a_marker_past_the_supplied_sources_is_dropped_and_counted() -> None:
    """Resolving it would attribute a claim to a passage the model was never shown, which is
    exactly what FR-CIT-06(2) forbids; rendering it would put a dead chip in front of a user.
    Dropping it is the only option that is neither."""
    segments, dropped = split_answer_segments("Refunds take 30 days [S9].", IDS)
    assert dropped == 1
    assert [segment.chunk_id for segment in segments] == [None]
    assert "".join(segment.text for segment in segments) == "Refunds take 30 days ."


def test_a_zero_marker_is_dropped_because_the_scheme_is_one_based() -> None:
    segments, dropped = split_answer_segments("Refunds [S0] take 30 days.", IDS)
    assert dropped == 1
    assert all(segment.chunk_id is None for segment in segments)


def test_dropping_a_marker_does_not_leave_a_doubled_space() -> None:
    segments, _ = split_answer_segments("the claim [S9] and the next", IDS)
    assert "".join(segment.text for segment in segments) == "the claim and the next"


def test_text_the_model_wrote_is_never_otherwise_reflowed() -> None:
    """The answer is what the user reads. Only the space a removal actually doubled is taken."""
    answer = "a  deliberate   gap [S1] and\n\na paragraph break."
    segments, _ = split_answer_segments(answer, IDS)
    assert "".join(segment.text for segment in segments) == answer


def test_a_malformed_marker_is_left_as_prose() -> None:
    """`[S]` and `[Sx]` are not markers at all — the model wrote them, so they stay."""
    answer = "See [S] and [Sx] and [ S1 ]."
    segments, dropped = split_answer_segments(answer, IDS)
    assert dropped == 0
    assert segments == [AnswerSegment(text=answer)]


def test_every_marker_is_unresolvable_when_no_sources_were_supplied() -> None:
    segments, dropped = split_answer_segments("Grounded [S1].", [])
    assert dropped == 1
    assert cited_chunk_ids(segments) == []


def test_an_empty_answer_produces_no_segments() -> None:
    assert split_answer_segments("", IDS) == ([], 0)


# --- the cited set -------------------------------------------------------------


def test_cited_chunk_ids_are_distinct_and_in_first_citation_order() -> None:
    """FR-MSG-04's source line lists the documents an answer used, in the order the reader
    meets them — not in the reranker's ranking, which would name the last-cited document
    first."""
    segments, _ = split_answer_segments("Third [S3]. First [S1]. Third again [S3].", IDS)
    assert cited_chunk_ids(segments) == [IDS[2], IDS[0]]


def test_an_uncited_answer_cites_nothing() -> None:
    segments, _ = split_answer_segments("I cannot ground an answer to that.", IDS)
    assert cited_chunk_ids(segments) == []


# --- the prompt hand-off (R-44(3), R-44(7)) -----------------------------------


def test_the_payload_is_the_composer_s_and_is_not_re_composed() -> None:
    """R-44(7): T-307 supplies history and makes the call; the shape stays `prompts.py`'s."""
    composed = compose_answer_prompt(
        query=QUERY,
        sources=_sources(),
        history=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}],
    )
    roles = [message["role"] for message in composed.messages]
    assert roles == ["system", "user", "assistant", "user", "user"]
    assert composed.messages[0]["content"] == SYSTEM_PROMPT
    assert composed.messages[-1]["content"] == QUERY
    assert composed.messages[-2]["content"].startswith(CONTEXT_FENCE_OPEN)
    assert composed.messages[-2]["content"].endswith(CONTEXT_FENCE_CLOSE)
    assert composed.source_ids == tuple(IDS)


def test_the_system_message_carries_no_untrusted_bytes() -> None:
    """Property 1 of R-44(3), re-asserted at this call site rather than trusted from T-303:
    this is the third place in the system that puts retrieved text before a model."""
    composed = compose_answer_prompt(query=QUERY, sources=_sources())
    system = composed.messages[0]["content"]
    assert QUERY not in system
    for source in _sources():
        assert source.text not in system
        assert source.filename not in system
        assert source.locator not in system


def test_the_source_order_is_the_grounding_order() -> None:
    """The R-48(5) invariant at its origin: markers are assigned in the order the sources are
    supplied, so `[S<k>]` addresses `reranked_chunk_ids[k-1]` and the mapping survives the node
    boundary a `ComposedPrompt` cannot cross."""
    sources = _sources()
    composed = compose_answer_prompt(query=QUERY, sources=sources)
    context = composed.messages[-2]["content"]
    positions = [context.index(source_marker(index)) for index in range(1, 4)]
    assert positions == sorted(positions)
    assert composed.source_ids == tuple(str(source.chunk_id) for source in sources)


# --- generate_answer: the happy path ------------------------------------------


async def test_a_streamed_answer_is_buffered_whole_with_its_metering() -> None:
    """NFR-PRF-02 puts the FR-CIT-06 gate between this and the client, so nothing is served
    until the answer is complete — `generate_answer` returns text, never a stream."""
    chat = FakeChatClient(answer="Refunds take 30 days [S1].")
    result = await generate_answer(query=QUERY, sources=_sources(), chat=chat, settings=_settings())

    assert result.text == "Refunds take 30 days [S1]."
    assert result.source_ids == tuple(IDS)
    assert result.model == "fake-chat"
    assert result.prompt_tokens and result.completion_tokens


async def test_the_default_double_cites_only_markers_it_was_given() -> None:
    """The fake must exercise the parser, or a broken segmentation passes CI unnoticed —
    and it must never mint a marker the context did not carry, or R-48(6)'s drop rule would
    look like the normal path."""
    chat = FakeChatClient()
    result = await generate_answer(query=QUERY, sources=_sources(), chat=chat, settings=_settings())
    segments, dropped = split_answer_segments(result.text, result.source_ids)

    assert dropped == 0
    assert cited_chunk_ids(segments) == IDS


async def test_history_reaches_the_model_as_ordinary_turns() -> None:
    """OI-32, revisited at T-307 and left as it stands: a prior answer re-enters as trusted
    `assistant` speech, outside the fence. Recorded here so a change is a deliberate one."""
    chat = FakeChatClient(answer="ok [S1]")
    await generate_answer(
        query=QUERY,
        sources=_sources(),
        history=[{"role": "assistant", "content": "an earlier answer"}],
        chat=chat,
        settings=_settings(),
    )

    sent = chat.calls[0]
    assert {"role": "assistant", "content": "an earlier answer"} in sent
    fenced = [message for message in sent if message["content"].startswith(CONTEXT_FENCE_OPEN)]
    assert "an earlier answer" not in fenced[0]["content"]


async def test_the_answer_budget_comes_from_the_generation_settings() -> None:
    captured: dict[str, int] = {}

    class _Recording(FakeChatClient):
        async def stream_answer(self, messages, *, max_output_tokens, model=None):  # type: ignore[no-untyped-def]
            captured["max_output_tokens"] = max_output_tokens
            return await super().stream_answer(
                messages, max_output_tokens=max_output_tokens, model=model
            )

    await generate_answer(
        query=QUERY,
        sources=_sources(),
        chat=_Recording(answer="ok [S1]"),
        settings=_settings(max_output_tokens=321),
    )
    assert captured["max_output_tokens"] == 321


# --- generate_answer: fail-closed (R-48(2)) -----------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ChatUnavailableError("the provider is down"),
        ChatRefusedError("I can't help with that"),
        ChatResponseError("empty"),
    ],
)
async def test_every_model_failure_propagates(error: Exception) -> None:
    """The deliberate inverse of `rerank_passages`' never-raises contract (R-47(2)).

    `route` and `rerank` fail open because each has a defensible degraded output — `hybrid`,
    and the R-46(3) merged order. Generation's degraded output would be an answer that is not
    grounded, which R-23, FR-SYS-02 and FR-RET-05 forbid in the same breath. The contract lives
    in this interface so a future call site inherits it rather than having to remember it.
    """
    with pytest.raises(type(error)):
        await generate_answer(
            query=QUERY,
            sources=_sources(),
            chat=FakeChatClient(error=error),
            settings=_settings(),
        )


async def test_an_empty_answer_is_a_failure_and_not_an_abstention() -> None:
    """R-23's abstention is a *decision* the graph makes, with copy of its own. Silence from
    the provider is not that, and dressing it up as one would report an outage to the user as
    a grounded answer."""
    with pytest.raises(ChatResponseError):
        await generate_answer(
            query=QUERY,
            sources=_sources(),
            chat=FakeChatClient(answer="   "),
            settings=_settings(),
        )


# --- live API (skipped without a key) -----------------------------------------


def _live_chat():  # noqa: ANN202
    from app.config import get_settings
    from app.services.llm import OpenAIChatClient

    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live generation tests skipped")
    return OpenAIChatClient(settings), settings


#: A small corpus with one unambiguous answer in it, and two on-topic passages that do not
#: contain it. If the model cites the wrong marker, the chip a user hovers shows the wrong
#: passage — the failure no mocked test can see, because a double supplies the very judgement
#: under evaluation (the T-304 lesson).
_LIVE_PASSAGES = [
    ("returns-hours.pdf", "Our returns desk is open Monday to Friday, 9am to 5pm."),
    (
        "damaged-goods.pdf",
        "Damaged goods must be reported within 48 hours. The refund window for damaged "
        "goods is 14 days from delivery, after which only a store credit is offered.",
    ),
    ("returns-overview.pdf", "This guide covers returns, refunds and exchanges in general terms."),
]


def _live_sources() -> list[PromptSource]:
    return [
        PromptSource(chunk_id=f"c{index}", filename=filename, text=text, locator=f"p. {index + 1}")
        for index, (filename, text) in enumerate(_LIVE_PASSAGES)
    ]


def _live_diagnostic(result, segments, dropped: int) -> str:  # noqa: ANN001
    """Everything needed to diagnose a live citation failure, in one assertion message.

    T-313 exists because this test failed twice and **the failure message was never
    captured**, leaving a real observation unusable — nine clean full runs later the cause is
    still unknown, and the only cheap insurance is that the *next* occurrence explains itself.
    A live assertion about model behaviour is a probability, not a property, so it will
    eventually fail again; what must not happen again is failing without evidence.
    """
    return (
        f"\n  cited      : {cited_chunk_ids(segments)}"
        f"\n  dropped    : {dropped}"
        f"\n  model      : {result.model}"
        f"\n  tokens     : prompt={result.prompt_tokens} completion={result.completion_tokens}"
        f"\n  source_ids : {list(result.source_ids)}"
        f"\n  answer     : {result.text!r}"
    )


async def test_live_the_answer_cites_the_passage_that_supports_it() -> None:
    """The only test that can show the citation contract works at all.

    `SYSTEM_PROMPT` *asks* for `[S<n>]` markers; whether a real model obliges — and whether it
    picks the right one — is not something a fake can vouch for. If this fails, the prompt is
    wrong, not the parser.

    **The strict `== ["c1"]` is deliberate and was re-confirmed by measurement (T-313).** The
    fixture deliberately includes a third, on-topic-but-non-answering passage, so the obvious
    reading — that the assertion is over-strict and a good answer citing the overview too
    would fail it — was the leading hypothesis for why this test was once intermittently red.
    It is **not supported**: over **26 consecutive live runs** of this exact call, `gpt-4o`
    cited `["c1"]` and nothing else, every time. Loosening it to "cited first" would therefore
    weaken a test that is measurably accurate, so the assertion stays and the diagnostics get
    better instead. (R-48's original "5/5" is superseded by that 26/26.)
    """
    chat, settings = _live_chat()
    try:
        result = await generate_answer(
            query="How long do I have to claim a refund on damaged goods?",
            sources=_live_sources(),
            chat=chat,
            settings=settings,
        )
    finally:
        await chat.aclose()

    segments, dropped = split_answer_segments(result.text, result.source_ids)
    report = _live_diagnostic(result, segments, dropped)
    assert dropped == 0, f"the model cited a source it was not given:{report}"
    assert "14 days" in result.text, f"the answer does not carry the fact it was asked for:{report}"
    # The answering passage is the second one, so a correct citation resolves to `c1`.
    assert cited_chunk_ids(segments) == ["c1"], f"wrong or extra citation:{report}"
    assert result.prompt_tokens and result.completion_tokens, f"usage was not reported:{report}"


async def test_live_an_unsupported_question_is_declined_rather_than_answered() -> None:
    """R-23 at the prompt level, not just at the empty-scope branch.

    The empty-scope abstention is decided in `graph.py` and never reaches a model. This is the
    other half: a *populated* context that simply does not support the question. FR-SYS-02 and
    FR-RET-05 both forbid falling back on pre-training here, and only the system prompt can
    enforce it.
    """
    chat, settings = _live_chat()
    try:
        result = await generate_answer(
            query="What is the capital of France?",
            sources=_live_sources(),
            chat=chat,
            settings=settings,
        )
    finally:
        await chat.aclose()

    assert "Paris" not in result.text, f"answered from pre-training: {result.text!r}"
    segments, _ = split_answer_segments(result.text, result.source_ids)
    assert cited_chunk_ids(segments) == [], result.text


async def test_live_generation_fails_closed_on_an_unknown_model() -> None:
    """R-48(2) against a real 404 rather than an injected exception — the deliberate opposite
    of the reranker's live fallback test."""
    from app.rag.errors import FailureClass, classify
    from app.services.llm import ChatError, OpenAIChatClient

    _, settings = _live_chat()
    broken = settings.model_copy(
        update={"openai": settings.openai.model_copy(update={"chat_model": "no-such-model"})}
    )
    chat = OpenAIChatClient(broken)
    try:
        with pytest.raises(ChatError) as caught:
            await generate_answer(
                query="refund window?", sources=_live_sources(), chat=chat, settings=settings
            )
    finally:
        await chat.aclose()

    assert classify(caught.value) is FailureClass.LLM_ERROR


# --- the import guard ----------------------------------------------------------


def test_generation_module_imports_no_langgraph() -> None:
    """`app.rag.graph` calls `apply_strict_msgpack()` at import time; T-308's gate, T-402's
    persist step and T-309's evaluation job all need the parser without that side effect —
    the `errors.py` / `prompts.py` / `rerank.py` reason."""
    text = Path(generation_module.__file__).read_text(encoding="utf-8")
    for needle in ("langgraph", "langchain"):
        assert f"import {needle}" not in text and f"{needle} import" not in text, (
            f"app/rag/generation.py imports `{needle}` — it must stay reachable without "
            "triggering `apply_strict_msgpack()`"
        )
