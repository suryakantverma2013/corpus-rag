"""Chat-completions client seam (T-304, R-45(1)).

Nothing here touches Postgres. The OpenAI backend is exercised through `respx`, exactly as
`test_embeddings.py` exercises the sibling seam.

The response-shape tests carry the weight: every one of them is a failure a provider can
produce and the router then has to turn into `hybrid` rather than into a broken turn, so the
seam must raise a `ChatError` for each — a swallowed `None` here would reach `parse_decision`
as "no class" and be indistinguishable from a real classification of an unclassifiable query.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import LlmSettings, OpenAISettings, RouterSettings, Settings
from app.services import llm as llm_module
from app.services.llm import (
    ChatAuthError,
    ChatClient,
    ChatConfigError,
    ChatError,
    ChatRateLimitedError,
    ChatRefusedError,
    ChatRejectedError,
    ChatResponseError,
    ChatUnavailableError,
    FakeChatClient,
    OpenAIChatClient,
    build_chat_client,
    close_chat_client,
    get_chat_client,
)

_URL = "https://api.openai.com/v1/chat/completions"
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _settings(**llm: Any) -> Settings:
    return Settings(
        openai=OpenAISettings(api_key="test-key", router_model="gpt-4o-mini"),
        llm=LlmSettings(**llm),
        router=RouterSettings(),
    )


def _client(**llm: Any) -> OpenAIChatClient:
    return OpenAIChatClient(_settings(**llm))


def _body(
    content: str | None = '{"ok": true}',
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": (
            [{"index": 0, "message": message, "finish_reason": finish_reason}] if choices else []
        ),
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


async def _complete(client: ChatClient, **kwargs: Any):  # noqa: ANN201
    try:
        return await client.complete_json(
            [{"role": "user", "content": "hi"}],
            schema=_SCHEMA,
            schema_name="probe",
            max_output_tokens=kwargs.pop("max_output_tokens", 64),
            **kwargs,
        )
    finally:
        await client.aclose()


# --- the happy path ------------------------------------------------------------


async def test_a_structured_response_is_parsed_with_its_metering(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body())
    result = await _complete(_client())

    assert result.data == {"ok": True}
    assert result.model == "gpt-4o-mini"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7


async def test_the_request_asks_for_a_strict_schema_and_sends_no_temperature(
    respx_mock: Any,
) -> None:
    """Both halves are decisions, not defaults.

    `strict` is what makes the enum in `ROUTER_SCHEMA` load-bearing. Omitting `temperature` is
    the deliberate one: newer model families reject any value but the default, and that
    rejection is a 400 — which R-45(2) converts into a silent fail-open to `hybrid`. A
    parameter that can disable the whole feature when someone edits `OPENAI_ROUTER_MODEL` is
    not worth the determinism it buys under a strict schema.
    """
    route = respx_mock.post(_URL).respond(json=_body())
    await _complete(_client())

    request = route.calls.last.request
    import json

    payload = json.loads(request.content)
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["name"] == "probe"
    assert payload["max_completion_tokens"] == 64
    assert "temperature" not in payload


# --- response shapes a provider can actually produce --------------------------


async def test_a_refusal_is_its_own_error(respx_mock: Any) -> None:
    """Not folded into `ChatResponseError`: T-307 must be able to tell "the model declined"
    from "the model answered and we could not read it"."""
    respx_mock.post(_URL).respond(json=_body(None, refusal="I can't help with that"))
    with pytest.raises(ChatRefusedError):
        await _complete(_client())


async def test_a_truncated_response_names_the_ceiling(respx_mock: Any) -> None:
    """The operator's fix here is a number (`ROUTER_MAX_OUTPUT_TOKENS`), not an
    investigation — so it must not arrive as a generic parse failure."""
    respx_mock.post(_URL).respond(json=_body('{"ok": tr', finish_reason="length"))
    with pytest.raises(ChatResponseError, match="ceiling"):
        await _complete(_client())


@pytest.mark.parametrize("content", [None, "", "   ", "not json at all", "[1, 2]", '"a string"'])
async def test_every_unusable_body_raises(respx_mock: Any, content: str | None) -> None:
    """A JSON *array* and a bare string both parse and are both wrong: `complete_json` promises
    an object, and a caller doing `data.get(...)` on a list raises far from here."""
    respx_mock.post(_URL).respond(json=_body(content))
    with pytest.raises(ChatResponseError):
        await _complete(_client())


async def test_no_choices_raises(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body(choices=False))
    with pytest.raises(ChatResponseError):
        await _complete(_client())


# --- error translation --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ChatRejectedError),
        (401, ChatAuthError),
        (403, ChatAuthError),
        (404, ChatConfigError),
        (422, ChatRejectedError),
        (429, ChatRateLimitedError),
        (500, ChatUnavailableError),
        (503, ChatUnavailableError),
        (409, ChatUnavailableError),
    ],
)
async def test_status_codes_translate(
    respx_mock: Any, status: int, expected: type[ChatError]
) -> None:
    """The seam exists to abstract the provider, so nothing above it should ever see an
    `openai.*` exception — the T-213 lesson, where a leaked botocore sibling turned a
    normative 503 into a 500."""
    respx_mock.post(_URL).respond(status_code=status, json={"error": {"message": "no"}})
    client = _client(router_max_retries=0)
    with pytest.raises(expected):
        await _complete(client)


async def test_an_exhausted_quota_is_terminal_not_throttling(respx_mock: Any) -> None:
    """A 429 is *usually* throttling, but it is also how exhausted credit is reported, and
    that never clears on its own — the T-205 finding, restated for this endpoint."""
    respx_mock.post(_URL).respond(
        status_code=429, json={"error": {"message": "quota", "code": "insufficient_quota"}}
    )
    client = _client(router_max_retries=0)
    with pytest.raises(ChatConfigError):
        await _complete(client)


async def test_a_connection_failure_translates(respx_mock: Any) -> None:
    import httpx

    respx_mock.post(_URL).mock(side_effect=httpx.ConnectError("down"))
    client = _client(router_max_retries=0)
    with pytest.raises(ChatUnavailableError):
        await _complete(client)


async def test_every_error_carries_a_code_the_failure_classes_know() -> None:
    """`app.rag.errors.classify` maps by `exc.code`, and translating an SDK error is what stops
    it being an `openai.OpenAIError` — so a missing entry files a generation failure as
    `SYSTEM_FAILURE`, the exact defect T-302 fixed for `type(exc).__name__`."""
    from app.rag.errors import FailureClass, classify

    subclasses = [
        ChatConfigError,
        ChatAuthError,
        ChatRejectedError,
        ChatRefusedError,
        ChatResponseError,
        ChatRateLimitedError,
        ChatUnavailableError,
        ChatError,
    ]
    for error_type in subclasses:
        assert classify(error_type("x")) is not FailureClass.SYSTEM_FAILURE, error_type.__name__
    assert classify(ChatRateLimitedError("x")) is FailureClass.RATE_LIMITED


# --- configuration ------------------------------------------------------------


async def test_a_missing_key_refuses_rather_than_falling_back_to_the_fake() -> None:
    """Silently routing every query with a stub would be indistinguishable from working."""
    client = OpenAIChatClient(Settings(openai=OpenAISettings(api_key=""), llm=LlmSettings()))
    with pytest.raises(ChatConfigError, match="LLM_BACKEND=fake"):
        await _complete(client)


def test_the_backend_is_selected_explicitly() -> None:
    assert isinstance(build_chat_client(_settings(backend="fake")), FakeChatClient)
    assert isinstance(build_chat_client(_settings(backend="openai")), OpenAIChatClient)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"router_timeout_seconds": -1},
        {"connect_timeout_seconds": 0},
        {"max_retries": -1},
        {"router_max_retries": -1},
    ],
)
def test_incoherent_transport_settings_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="LLM_"):
        LlmSettings(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_sub_queries": -1},
        {"max_probe_chars": 0},
        {"max_output_tokens": 0},
        {"history_turns": -1},
        {"history_max_chars": 0},
    ],
)
def test_incoherent_router_settings_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="ROUTER_"):
        RouterSettings(**kwargs)


async def test_the_cached_client_is_dropped_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "build_chat_client", lambda: FakeChatClient())
    first = get_chat_client()
    assert get_chat_client() is first
    await close_chat_client()
    assert llm_module._client is None  # noqa: SLF001 — the point of the test


# --- per-call-site model and budget (T-306, R-47) ------------------------------


async def test_rerank_json_calls_the_rerank_model_not_the_router_model(
    respx_mock: Any,
) -> None:
    """One seam, two call sites, two model ids — the reason `rerank_json` exists at all."""
    import json

    route = respx_mock.post(_URL).respond(json=_body())
    settings = Settings(
        openai=OpenAISettings(
            api_key="test-key", router_model="gpt-4o-mini", rerank_model="gpt-4.1-mini"
        ),
        llm=LlmSettings(),
        router=RouterSettings(),
    )
    client = OpenAIChatClient(settings)
    try:
        await client.rerank_json(
            [{"role": "user", "content": "hi"}],
            schema=_SCHEMA,
            schema_name="passage_relevance",
            max_output_tokens=64,
        )
    finally:
        await client.aclose()

    assert json.loads(route.calls.last.request.content)["model"] == "gpt-4.1-mini"


async def test_each_call_site_applies_its_own_timeout(respx_mock: Any) -> None:
    """The budgets must not be baked into the constructor, or one client cannot serve both.

    Asserted on the request's own timeout extension rather than by counting seconds: the
    failure this guards against is the reranker silently inheriting the router's 8-second
    leash, which no assertion on the response could see.
    """
    route = respx_mock.post(_URL).respond(json=_body())
    client = OpenAIChatClient(_settings(router_timeout_seconds=3.0, rerank_timeout_seconds=17.0))
    try:
        await client.complete_json(
            [{"role": "user", "content": "hi"}],
            schema=_SCHEMA,
            schema_name="query_route",
            max_output_tokens=64,
        )
        router_timeout = route.calls.last.request.extensions["timeout"]["read"]
        await client.rerank_json(
            [{"role": "user", "content": "hi"}],
            schema=_SCHEMA,
            schema_name="passage_relevance",
            max_output_tokens=64,
        )
        rerank_timeout = route.calls.last.request.extensions["timeout"]["read"]
    finally:
        await client.aclose()

    assert router_timeout == 3.0
    assert rerank_timeout == 17.0


def test_incoherent_rerank_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="LLM_"):
        LlmSettings(rerank_timeout_seconds=0)
    with pytest.raises(ValueError, match="LLM_"):
        LlmSettings(rerank_max_retries=-1)


def test_incoherent_generation_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="LLM_MAX_OUTPUT_TOKENS"):
        LlmSettings(max_output_tokens=0)


# --- streaming generation (T-307, R-48) ----------------------------------------


def _sse(*chunks: dict[str, Any]) -> str:
    """A `text/event-stream` body the SDK's own parser accepts."""
    import json

    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def _delta_chunk(
    content: str | None = None,
    *,
    finish_reason: str | None = None,
    refusal: str | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if refusal is not None:
        delta["refusal"] = refusal
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage_chunk(prompt: int = 40, completion: int = 9) -> dict[str, Any]:
    """The trailing usage-only chunk `stream_options={"include_usage": True}` asks for."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-4o",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _stream_response(respx_mock: Any, body: str) -> Any:
    return respx_mock.post(_URL).respond(text=body, headers={"content-type": "text/event-stream"})


def _generation_settings(**llm: Any) -> Settings:
    return Settings(
        openai=OpenAISettings(api_key="test-key", chat_model="gpt-4o"),
        llm=LlmSettings(**llm),
        router=RouterSettings(),
    )


async def test_a_streamed_answer_concatenates_its_deltas(respx_mock: Any) -> None:
    _stream_response(
        respx_mock,
        _sse(
            _delta_chunk("Refunds "),
            _delta_chunk("take 30 days "),
            _delta_chunk("[S1]."),
            _delta_chunk(finish_reason="stop"),
            _usage_chunk(),
        ),
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=64
        )
        result = await stream.collect()
    finally:
        await client.aclose()

    assert result.text == "Refunds take 30 days [S1]."
    assert result.model == "gpt-4o"


async def test_streaming_asks_for_usage_and_gets_the_metering(respx_mock: Any) -> None:
    """Without `stream_options`, the SDK streams no `usage` at all — and `prompt_tokens` /
    `completion_tokens` (FR-MSG-06, the FR-ANL cards, OI-16's per-chat accounting) would be
    silently `None` on every real answer while every mocked test that supplied them stayed
    green. That is why both halves are asserted here."""
    import json

    route = _stream_response(
        respx_mock, _sse(_delta_chunk("ok"), _delta_chunk(finish_reason="stop"), _usage_chunk())
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=64
        )
        result = await stream.collect()
    finally:
        await client.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["model"] == "gpt-4o"
    assert payload["max_completion_tokens"] == 64
    # No `temperature`, and it matters more here than at the other two call sites: this one
    # fails *closed*, so a model family rejecting the parameter would fail the turn outright.
    assert "temperature" not in payload
    assert (result.prompt_tokens, result.completion_tokens) == (40, 9)


async def test_generation_uses_its_own_budget_and_never_the_router_leash(
    respx_mock: Any,
) -> None:
    """R-48(1): an 8-second cap tuned for a classification that fails open would cut off a
    long grounded answer that fails closed."""
    route = _stream_response(
        respx_mock, _sse(_delta_chunk("ok"), _delta_chunk(finish_reason="stop"))
    )
    client = OpenAIChatClient(
        _generation_settings(timeout_seconds=75.0, router_timeout_seconds=3.0)
    )
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=8
        )
        await stream.collect()
    finally:
        await client.aclose()

    assert route.calls.last.request.extensions["timeout"]["read"] == 75.0


async def test_a_streamed_refusal_is_its_own_error(respx_mock: Any) -> None:
    """ "The model declined" and "the model answered and we could not read it" call for
    different handling, and only the first is worth showing a user as anything but a system
    failure — R-45's reason for the class, at the streaming call site."""
    _stream_response(
        respx_mock,
        _sse(
            _delta_chunk(refusal="I can't help with that"),
            _delta_chunk(finish_reason="stop"),
        ),
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=64
        )
        with pytest.raises(ChatRefusedError, match="can't help"):
            await stream.collect()
    finally:
        await client.aclose()


async def test_a_truncated_answer_is_a_failure_not_a_short_answer(respx_mock: Any) -> None:
    """A truncated *grounded* answer is worse than none: it can drop the citation for a claim
    it already made, which FR-CIT-06 would reject anyway. The operator's fix is a number."""
    _stream_response(
        respx_mock,
        _sse(_delta_chunk("Refunds take"), _delta_chunk(finish_reason="length"), _usage_chunk()),
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=4
        )
        with pytest.raises(ChatResponseError, match="truncated"):
            await stream.collect()
    finally:
        await client.aclose()


async def test_an_empty_stream_raises_rather_than_returning_nothing(respx_mock: Any) -> None:
    _stream_response(respx_mock, _sse(_delta_chunk(finish_reason="stop")))
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=64
        )
        with pytest.raises(ChatResponseError, match="empty"):
            await stream.collect()
    finally:
        await client.aclose()


async def test_a_failure_mid_stream_is_translated_like_one_at_call_time(
    respx_mock: Any,
) -> None:
    """The `try` wraps the **iteration**, not just the request.

    A stream that dies after the first token raises from `__anext__`, and an untranslated
    error there would miss the taxonomy `app.rag.errors.classify` maps by `code` — a mid-stream
    rate limit would lose `RATE_LIMITED`'s "wait a moment" copy entirely.
    """
    import httpx

    from app.rag.errors import FailureClass, classify

    # A body that ends without `[DONE]` and with a broken line is what a dropped connection
    # looks like to the SDK's parser.
    respx_mock.post(_URL).mock(
        side_effect=httpx.ReadError("connection reset while streaming"),
    )
    client = OpenAIChatClient(_generation_settings(max_retries=0))
    try:
        with pytest.raises(ChatError) as caught:
            stream = await client.stream_answer(
                [{"role": "user", "content": "hi"}], max_output_tokens=64
            )
            await stream.collect()
    finally:
        await client.aclose()

    assert classify(caught.value) is not FailureClass.SYSTEM_FAILURE


async def test_the_stream_is_single_pass(respx_mock: Any) -> None:
    """`collect()` after a partial iteration returns what was actually received, rather than
    silently restarting a stream the provider has already closed."""
    _stream_response(
        respx_mock,
        _sse(_delta_chunk("one "), _delta_chunk("two"), _delta_chunk(finish_reason="stop")),
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}], max_output_tokens=64
        )
        seen = [delta async for delta in stream]
        result = await stream.collect()
    finally:
        await client.aclose()

    assert seen == ["one ", "two"]
    assert result.text == "one two"


async def test_the_fake_streams_an_answer_citing_only_supplied_markers() -> None:
    """`LLM_BACKEND=fake` must exercise the T-307 path — a double that answered in plain prose
    would let a broken segment parser pass CI, and one that invented `[S9]` would make
    R-48(6)'s drop rule look like the normal path."""
    client = FakeChatClient()
    messages = [
        {"role": "system", "content": "instructions"},
        {
            "role": "user",
            "content": "<<<CORPUS_UNTRUSTED_DOCUMENT_CONTEXT>>>\n[S1] filename=a.pdf\nthirty days"
            "\n\n[S2] filename=b.pdf\nholiday hours\n<<<END_CORPUS_UNTRUSTED_DOCUMENT_CONTEXT>>>",
        },
        {"role": "user", "content": "what is the refund window?"},
    ]
    stream = await client.stream_answer(messages, max_output_tokens=64)
    seen = [delta async for delta in stream]
    result = await stream.collect()

    assert "[S1]" in result.text and "[S2]" in result.text
    assert "[S3]" not in result.text
    # Several deltas, so a marker crosses a boundary — the arrangement that breaks a parser
    # written against whole chunks.
    assert len(seen) > 1
    assert result.prompt_tokens and result.completion_tokens


async def test_the_fake_streams_a_fixed_answer_when_given_one() -> None:
    client = FakeChatClient(answer="exactly this [S1]")
    stream = await client.stream_answer([{"role": "user", "content": "hi"}], max_output_tokens=64)
    assert (await stream.collect()).text == "exactly this [S1]"


async def test_the_fake_raises_on_the_streaming_path_too() -> None:
    client = FakeChatClient(error=ChatUnavailableError("down"))
    with pytest.raises(ChatUnavailableError):
        await client.stream_answer([{"role": "user", "content": "hi"}], max_output_tokens=64)


# --- the deterministic double -------------------------------------------------


async def test_the_fake_is_deterministic_and_records_its_calls() -> None:
    """Determinism is load-bearing: `tests/test_router.py` and the graph tests both assert on
    what it returns, so an unstable double would make them flaky rather than wrong."""
    client = FakeChatClient()
    messages = [{"role": "user", "content": "what is the refund window and who approves it?"}]
    first = await client.complete_json(
        messages, schema=_SCHEMA, schema_name="query_route", max_output_tokens=64
    )
    second = await client.complete_json(
        messages, schema=_SCHEMA, schema_name="query_route", max_output_tokens=64
    )

    assert first.data == second.data
    assert len(client.calls) == 2


async def test_the_fake_scores_passages_for_the_rerank_schema() -> None:
    """`LLM_BACKEND=fake` must exercise the T-306 path, not fall through to `{}`.

    And it must **discriminate** — a double that scored everything alike would let a broken
    ordering pass every test that uses it.
    """
    client = FakeChatClient(score_scale=10)
    messages = [
        {"role": "system", "content": "rubric"},
        {
            "role": "user",
            "content": "[S1] filename=a.pdf\nthe refund window is 30 days\n\n"
            "[S2] filename=b.pdf\nholiday opening hours",
        },
        {"role": "user", "content": "what is the refund window?"},
    ]
    result = await client.rerank_json(
        messages, schema=_SCHEMA, schema_name="passage_relevance", max_output_tokens=64
    )

    scores = {entry["id"]: entry["score"] for entry in result.data["scores"]}
    assert set(scores) == {1, 2}
    assert scores[1] > scores[2]


async def test_the_fake_answers_an_unknown_schema_with_an_empty_object() -> None:
    """It knows two schemas. Anything else must degrade to something the caller's own validation
    rejects, rather than to a plausible-looking answer for a schema it never saw."""
    client = FakeChatClient()
    result = await client.complete_json(
        [{"role": "user", "content": "x"}],
        schema=_SCHEMA,
        schema_name="something_else",
        max_output_tokens=64,
    )
    assert result.data == {}


# --- live API (skipped without a key) -----------------------------------------


def _live_client() -> OpenAIChatClient:
    from app.config import get_settings

    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live chat tests skipped")
    return OpenAIChatClient(settings)


async def test_live_router_call_returns_a_valid_class() -> None:
    """The only test that can catch a real model/parameter misconfiguration — a schema the API
    rejects, or a `max_completion_tokens`/`response_format` combination this model does not
    support. Every unit test above mocks the transport and so cannot."""
    from app.rag.router import QUERY_CLASSES as CLASSES
    from app.rag.router import ROUTER_SCHEMA, ROUTER_SCHEMA_NAME, compose_router_messages

    client = _live_client()
    try:
        result = await client.complete_json(
            compose_router_messages(query="What is the refund window and who approves it?"),
            schema=ROUTER_SCHEMA,
            schema_name=ROUTER_SCHEMA_NAME,
            max_output_tokens=800,
        )
    finally:
        await client.aclose()

    assert result.data["query_class"] in CLASSES
    assert isinstance(result.data["probes"], list)


# --- runtime model overrides (T-611, R-83) ------------------------------------


async def test_a_per_call_model_overrides_the_configured_one_at_every_call_site(
    respx_mock: Any,
) -> None:
    """The operator's runtime selection has to reach the wire, not merely the seam.

    Asserted against the real client rather than `FakeChatClient`, because the graph-level
    wiring test drives the fake and so cannot see this line at all — a mutation dropping
    `model or ...` here left that test green, which is what this exists to catch.
    """
    import json

    for method, kwargs in (
        ("complete_json", {"schema": _SCHEMA, "schema_name": "s", "max_output_tokens": 64}),
        ("rerank_json", {"schema": _SCHEMA, "schema_name": "s", "max_output_tokens": 64}),
        ("evaluate_json", {"schema": _SCHEMA, "schema_name": "s", "max_output_tokens": 64}),
    ):
        route = respx_mock.post(_URL).respond(json=_body())
        client = _client()
        try:
            await getattr(client, method)(
                [{"role": "user", "content": "hi"}], model="operator-choice", **kwargs
            )
        finally:
            await client.aclose()
        sent = json.loads(route.calls.last.request.content)["model"]
        assert sent == "operator-choice", f"{method} sent {sent!r}"


async def test_a_streamed_answer_requests_the_overridden_model(respx_mock: Any) -> None:
    """The override reaches the wire.

    What is *reported* is deliberately not asserted here: the provider echoes the model it
    resolved in every chunk — usually a dated snapshot such as `gpt-4o-2024-08-06` — and
    `AnswerStream` prefers that echo, which is the more precise record for
    `messages.model_name`. The next test pins the fallback when there is no echo.
    """
    import json

    _stream_response(
        respx_mock,
        _sse(_delta_chunk("Refunds take 30 days."), _delta_chunk(finish_reason="stop")),
    )
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}],
            max_output_tokens=64,
            model="operator-choice",
        )
        await stream.collect()
    finally:
        await client.aclose()

    assert json.loads(respx_mock.calls.last.request.content)["model"] == "operator-choice"


async def test_an_unechoed_stream_reports_the_overridden_model_not_the_configured_one(
    respx_mock: Any,
) -> None:
    """The fallback, and it is the half with a user-visible consequence.

    `AnswerStream.model` becomes `messages.model_name`. With no echo to correct it, a client
    that called the override but seeded the stream with `OPENAI_CHAT_MODEL` would name a
    model that did not write the answer — FR-ANL-02 pointing at the wrong thing, with
    nothing failing anywhere.
    """
    _stream_response(respx_mock, _sse({"choices": [{"index": 0, "delta": {"content": "hi"}}]}))
    client = OpenAIChatClient(_generation_settings())
    try:
        stream = await client.stream_answer(
            [{"role": "user", "content": "hi"}],
            max_output_tokens=64,
            model="operator-choice",
        )
        result = await stream.collect()
    finally:
        await client.aclose()

    assert result.model == "operator-choice"


async def test_omitting_the_model_keeps_the_configured_default(respx_mock: Any) -> None:
    """The whole compatibility guarantee: a caller that knows nothing of T-611 is unchanged."""
    import json

    route = respx_mock.post(_URL).respond(json=_body())
    client = _client()
    try:
        await client.complete_json(
            [{"role": "user", "content": "hi"}],
            schema=_SCHEMA,
            schema_name="s",
            max_output_tokens=64,
        )
    finally:
        await client.aclose()

    assert json.loads(route.calls.last.request.content)["model"] == "gpt-4o-mini"


async def test_verify_model_asks_the_provider_and_translates_its_refusal(
    respx_mock: Any,
) -> None:
    """R-83(3)'s probe. A 404 from the provider must arrive as a `ChatError`, or the CLI
    cannot tell "no such model" from a bug and would persist the id anyway."""
    respx_mock.get("https://api.openai.com/v1/models/gtp-4o").respond(
        status_code=404, json={"error": {"message": "The model does not exist"}}
    )
    client = _client()
    try:
        with pytest.raises(ChatError):
            await client.verify_model("gtp-4o")
    finally:
        await client.aclose()
