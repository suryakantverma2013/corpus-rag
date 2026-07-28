"""Embedding client seam (T-205, FR-ING-03).

Nothing here touches Postgres. The OpenAI backend is exercised through `respx`, the same
way `test_admin_users.py` exercises Keycloak — note that the SDK requests
``encoding_format="base64"`` by default and decodes client-side, so a success body must
carry base64 float32 bytes, not a JSON array of floats.
"""

from __future__ import annotations

import array
import base64
import inspect
import math
from typing import Any

import httpx
import pytest

from app.config import EMBEDDING_MAX_INPUT_CHARS, EmbeddingSettings, OpenAISettings, Settings
from app.db.base import EMBEDDING_DIM
from app.services import embeddings as embeddings_module
from app.services.embeddings import (
    EmbeddingAuthError,
    EmbeddingClient,
    EmbeddingConfigError,
    EmbeddingDimensionError,
    EmbeddingInputError,
    EmbeddingRateLimitedError,
    EmbeddingRejectedError,
    EmbeddingResponseError,
    EmbeddingUnavailableError,
    FakeEmbeddingClient,
    OpenAIEmbeddingClient,
    build_embedding_client,
    close_embedding_client,
    get_embedding_client,
    plan_batches,
)

_URL = "https://api.openai.com/v1/embeddings"


def _encode(vector: list[float]) -> str:
    """Base64 float32, exactly as the API returns it under the SDK's default format."""
    return base64.b64encode(array.array("f", vector).tobytes()).decode()


def _body(vectors: list[list[float]], *, indices: list[int] | None = None) -> dict[str, Any]:
    indices = list(range(len(vectors))) if indices is None else indices
    return {
        "object": "list",
        "model": "text-embedding-3-large",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
        "data": [
            {"object": "embedding", "index": index, "embedding": _encode(vector)}
            for index, vector in zip(indices, vectors, strict=True)
        ],
    }


def _vector(seed: float, dim: int = EMBEDDING_DIM) -> list[float]:
    return [seed] * dim


def _client(**embedding_kwargs: Any) -> OpenAIEmbeddingClient:
    """An OpenAI client with a dummy key and, by default, retries off (tests stay fast)."""
    embedding_kwargs.setdefault("max_retries", 0)
    embedding_kwargs.setdefault("query_max_retries", 0)
    return OpenAIEmbeddingClient(
        Settings(
            openai=OpenAISettings(api_key="test-key", embedding_model="text-embedding-3-large"),
            embedding=EmbeddingSettings(**embedding_kwargs),
        )
    )


# --- batch planning -----------------------------------------------------------


def test_plan_batches_splits_on_the_count_ceiling() -> None:
    assert plan_batches(["x"] * 5, max_batch_size=2, max_batch_chars=1_000_000) == [
        (0, 1),
        (2, 3),
        (4,),
    ]


def test_plan_batches_splits_on_the_char_ceiling() -> None:
    texts = ["a" * 400, "b" * 400, "c" * 400]
    assert plan_batches(texts, max_batch_size=100, max_batch_chars=800) == [(0, 1), (2,)]


def test_plan_batches_of_nothing_is_no_requests() -> None:
    assert plan_batches([], max_batch_size=8, max_batch_chars=100) == []


def test_plan_batches_never_drops_or_loops_on_an_oversized_text() -> None:
    # Unreachable given the pre-flight guard and the settings validator, but a planner
    # that can hang the worker is not worth the saving.
    texts = ["a" * 50, "b" * 500, "c" * 50]
    batches = plan_batches(texts, max_batch_size=10, max_batch_chars=100)
    assert sorted(index for batch in batches for index in batch) == [0, 1, 2]


def test_batching_never_consults_the_token_count_estimate() -> None:
    """R-35(7): `token_count` is a len/4 estimate; an underestimate is a hard 400."""
    source = inspect.getsource(embeddings_module)
    assert "estimate_token_count" not in source


# --- input validation ---------------------------------------------------------


async def test_blank_text_is_rejected_before_any_request() -> None:
    with pytest.raises(EmbeddingInputError):
        await FakeEmbeddingClient().embed_texts(["fine", "   "])


async def test_over_long_text_is_rejected_before_any_request() -> None:
    with pytest.raises(EmbeddingInputError) as excinfo:
        await FakeEmbeddingClient().embed_texts(["a" * (EMBEDDING_MAX_INPUT_CHARS + 1)])
    assert excinfo.value.retryable is False


# --- order, shape, dimensions -------------------------------------------------


async def test_response_order_is_taken_from_the_index_not_the_list(respx_mock: Any) -> None:
    """The single most important test here.

    A `zip(texts, response.data)` implementation passes every other assertion in this file
    and silently transposes the corpus: every vector is individually valid and every chunk
    is wrong. The API returns an `index` precisely because order is not contractual.
    """
    respx_mock.post(_URL).respond(
        json=_body([_vector(3.0), _vector(2.0), _vector(1.0)], indices=[2, 1, 0])
    )
    client = _client()
    try:
        result = await client.embed_texts(["first", "second", "third"])
    finally:
        await client.aclose()
    assert [row[0] for row in result] == [1.0, 2.0, 3.0]


async def test_a_duplicated_response_index_is_rejected(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body([_vector(1.0), _vector(2.0)], indices=[0, 0]))
    client = _client()
    try:
        with pytest.raises(EmbeddingResponseError):
            await client.embed_texts(["a", "b"])
    finally:
        await client.aclose()


async def test_an_out_of_range_response_index_is_rejected(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body([_vector(1.0)], indices=[7]))
    client = _client()
    try:
        with pytest.raises(EmbeddingResponseError):
            await client.embed_texts(["a"])
    finally:
        await client.aclose()


async def test_a_short_response_is_rejected(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body([_vector(1.0)]))
    client = _client()
    try:
        with pytest.raises(EmbeddingResponseError):
            await client.embed_texts(["a", "b"])
    finally:
        await client.aclose()


async def test_wrong_dimension_vectors_are_rejected_by_name(respx_mock: Any) -> None:
    """text-embedding-3-small is 1536-dim against a VECTOR(3072) column (caught in T-204)."""
    respx_mock.post(_URL).respond(json=_body([_vector(1.0, dim=1536)]))
    client = _client()
    try:
        with pytest.raises(EmbeddingDimensionError) as excinfo:
            await client.embed_texts(["a"])
    finally:
        await client.aclose()
    assert excinfo.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    assert excinfo.value.retryable is False
    assert "1536" in str(excinfo.value) and str(EMBEDDING_DIM) in str(excinfo.value)


async def test_multiple_batches_are_reassembled_in_input_order(respx_mock: Any) -> None:
    respx_mock.post(_URL).mock(
        side_effect=[
            httpx.Response(200, json=_body([_vector(1.0), _vector(2.0)])),
            httpx.Response(200, json=_body([_vector(3.0)])),
        ]
    )
    client = _client(max_batch_size=2, max_concurrent_requests=1)
    try:
        result = await client.embed_texts(["a", "b", "c"])
    finally:
        await client.aclose()
    assert [row[0] for row in result] == [1.0, 2.0, 3.0]


# --- error taxonomy -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (401, EmbeddingAuthError, False),
        (403, EmbeddingAuthError, False),
        (400, EmbeddingRejectedError, False),
        (422, EmbeddingRejectedError, False),
        (404, EmbeddingConfigError, False),
        (429, EmbeddingRateLimitedError, True),
        (500, EmbeddingUnavailableError, True),
        (503, EmbeddingUnavailableError, True),
    ],
)
async def test_status_codes_map_to_the_taxonomy(
    respx_mock: Any, status: int, expected: type[Exception], retryable: bool
) -> None:
    respx_mock.post(_URL).respond(status, json={"error": {"message": "nope", "code": "x"}})
    client = _client()
    try:
        with pytest.raises(expected) as excinfo:
            await client.embed_texts(["a"])
    finally:
        await client.aclose()
    assert excinfo.value.retryable is retryable


async def test_exhausted_quota_is_terminal_not_rate_limited(respx_mock: Any) -> None:
    """A 429 is usually throttling, but it is also how spent credit is reported.

    Classifying that as retryable stalls the whole queue for hours on a condition that
    never clears.
    """
    respx_mock.post(_URL).respond(
        429, json={"error": {"message": "quota", "code": "insufficient_quota"}}
    )
    client = _client()
    try:
        with pytest.raises(EmbeddingConfigError) as excinfo:
            await client.embed_texts(["a"])
    finally:
        await client.aclose()
    assert excinfo.value.retryable is False
    assert excinfo.value.code == "EMBEDDING_CONFIG"


async def test_a_connection_failure_is_retryable(respx_mock: Any) -> None:
    respx_mock.post(_URL).mock(side_effect=httpx.ConnectError("no route"))
    client = _client()
    try:
        with pytest.raises(EmbeddingUnavailableError) as excinfo:
            await client.embed_texts(["a"])
    finally:
        await client.aclose()
    assert excinfo.value.retryable is True


async def test_sdk_retries_are_actually_wired_to_the_client(respx_mock: Any) -> None:
    """Configuring `max_retries` on the wrong object is an easy and invisible mistake."""
    route = respx_mock.post(_URL).mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json=_body([_vector(1.0)])),
        ]
    )
    client = _client(max_retries=4)
    try:
        result = await client.embed_texts(["a"])
    finally:
        await client.aclose()
    assert len(result) == 1
    assert route.call_count == 3


async def test_a_missing_api_key_fails_loudly_rather_than_falling_back() -> None:
    """Silently embedding a corpus with fake vectors and reporting ACTIVE is far worse."""
    client = OpenAIEmbeddingClient(
        Settings(openai=OpenAISettings(api_key=""), embedding=EmbeddingSettings())
    )
    with pytest.raises(EmbeddingConfigError):
        await client.embed_texts(["a"])


async def test_embed_query_uses_the_shorter_budget(respx_mock: Any) -> None:
    respx_mock.post(_URL).respond(json=_body([_vector(4.0)]))
    client = _client(query_timeout_seconds=2.0)
    try:
        vector = await client.embed_query("what is corpus?")
    finally:
        await client.aclose()
    assert len(vector) == EMBEDDING_DIM
    assert vector[0] == 4.0


# --- fake backend -------------------------------------------------------------


async def test_fake_client_is_deterministic_across_instances() -> None:
    """Load-bearing: without it the fake cannot exercise the fingerprint diff."""
    first = await FakeEmbeddingClient().embed_texts(["a passage"])
    second = await FakeEmbeddingClient().embed_texts(["a passage"])
    assert first == second


async def test_fake_client_returns_unit_vectors_of_the_column_width() -> None:
    [vector] = await FakeEmbeddingClient().embed_texts(["hello"])
    assert len(vector) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


async def test_fake_client_distinguishes_texts_and_preserves_order() -> None:
    client = FakeEmbeddingClient()
    vectors = await client.embed_texts(["one", "two", "one"])
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
    assert client.embedded_inputs == 3


async def test_fake_client_of_nothing_costs_nothing() -> None:
    client = FakeEmbeddingClient()
    assert await client.embed_texts([]) == []
    assert client.request_count == 0


# --- factory / DI -------------------------------------------------------------


def test_both_backends_satisfy_the_protocol() -> None:
    assert isinstance(FakeEmbeddingClient(), EmbeddingClient)
    assert isinstance(OpenAIEmbeddingClient(Settings()), EmbeddingClient)


def test_backend_selection_is_explicit() -> None:
    fake = Settings(embedding=EmbeddingSettings(backend="fake"))
    real = Settings(embedding=EmbeddingSettings(backend="openai"))
    assert isinstance(build_embedding_client(fake), FakeEmbeddingClient)
    assert isinstance(build_embedding_client(real), OpenAIEmbeddingClient)


async def test_get_embedding_client_is_cached_until_closed(monkeypatch: Any) -> None:
    monkeypatch.setattr(embeddings_module, "_client", None)
    monkeypatch.setattr(embeddings_module, "build_embedding_client", lambda: FakeEmbeddingClient())
    first = get_embedding_client()
    assert get_embedding_client() is first
    await close_embedding_client()
    assert embeddings_module._client is None  # noqa: SLF001 — the point of the test


def test_settings_reject_a_batch_ceiling_below_one_chunk() -> None:
    # Below the input ceiling a legal maximum-size chunk fits in no batch at all.
    with pytest.raises(ValueError, match="EMBEDDING_MAX_BATCH_CHARS"):
        EmbeddingSettings(max_batch_chars=EMBEDDING_MAX_INPUT_CHARS - 1)


def test_settings_reject_a_batch_size_above_the_api_array_limit() -> None:
    with pytest.raises(ValueError, match="EMBEDDING_MAX_BATCH_SIZE"):
        EmbeddingSettings(max_batch_size=4096)


# --- live API (skipped without a key) -----------------------------------------


def _live_client() -> OpenAIEmbeddingClient:
    from app.config import get_settings

    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live embedding tests skipped")
    return OpenAIEmbeddingClient(settings)


async def test_live_query_matches_the_column_width() -> None:
    """The only test that can catch a real model/dimension misconfiguration."""
    client = _live_client()
    try:
        vector = await client.embed_query("Corpus readiness probe.")
    finally:
        await client.aclose()
    assert len(vector) == EMBEDDING_DIM


async def test_live_batch_is_aligned_and_deterministic() -> None:
    client = _live_client()
    try:
        first = await client.embed_texts(["alpha", "beta", "gamma"])
        second = await client.embed_texts(["alpha"])
    finally:
        await client.aclose()
    assert len(first) == 3
    assert all(len(vector) == EMBEDDING_DIM for vector in first)
    assert first[0] == second[0]
