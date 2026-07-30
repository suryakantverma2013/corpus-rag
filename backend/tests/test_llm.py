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


async def test_the_fake_answers_an_unknown_schema_with_an_empty_object() -> None:
    """It knows one schema. Anything else must degrade to something the caller's own validation
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
