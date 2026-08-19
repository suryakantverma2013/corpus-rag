"""OpenAI embedding client seam (T-205, FR-ING-03, R-15).

One `EmbeddingClient` protocol, two backends: the real OpenAI SDK and a deterministic
in-process fake for dev and CI.

**Why this lives in `services/` and not `ingestion/`.** Everything under `app/ingestion/`
is pure and deterministic — no clock, no RNG, no I/O, no session — and `tests/test_parsers.py`
carries a guard test that keeps it that way. This module is the opposite: a pooled HTTP
client bound to the running event loop and released in the app lifespan, which is the shape
of `object_storage.py` and `jobs.py`. It is also consumed outside ingestion — T-206 needs
:meth:`EmbeddingClient.embed_query` on the chat request path, and a route importing
`app.ingestion.*` would breach the R-31 "parse in the worker" boundary.

The client is deliberately dumb: it embeds exactly the texts it is given, in order, and
never dedupes. R-35(9)'s dedupe-the-calls rule belongs to the caller
(`app.ingestion.incremental`), which is the only layer that knows a chunk from a query.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
from collections.abc import Sequence
from typing import Annotated, ClassVar, Protocol, runtime_checkable

import httpx
import structlog
from fastapi import Depends

from app.config import EMBEDDING_MAX_INPUT_CHARS, Settings, get_settings
from app.db.base import EMBEDDING_DIM

log = structlog.get_logger(__name__)

__all__ = [
    "EmbeddingAuthError",
    "EmbeddingClient",
    "EmbeddingClientDep",
    "EmbeddingConfigError",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "EmbeddingInputError",
    "EmbeddingRateLimitedError",
    "EmbeddingRejectedError",
    "EmbeddingResponseError",
    "EmbeddingUnavailableError",
    "FakeEmbeddingClient",
    "OpenAIEmbeddingClient",
    "build_embedding_client",
    "close_embedding_client",
    "get_embedding_client",
    "plan_batches",
]

#: OpenAI rejects an input array longer than this in a single request.
_MAX_INPUTS_PER_REQUEST = 2048

#: 429 bodies carrying one of these `error.code` values are exhausted billing, not
#: throttling. They never resolve on their own — see :func:`_translate`.
_TERMINAL_429_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


# --- errors -------------------------------------------------------------------


class EmbeddingError(Exception):
    """An embedding request could not be completed.

    Mirrors `ParseError` (T-203): ``code`` is what T-207 writes to
    `knowledge_jobs.error_code` (`String(100)`) and ``str(exc)`` is the human message for
    `documents.error_message`. Unlike parse failures these are *not* uniformly terminal, so
    ``retryable`` is part of the contract — T-207's whole decision is
    ``if exc.retryable: raise`` (arq backs off and eventually dead-letters) else fail the
    document.
    """

    code: ClassVar[str] = "EMBEDDING_FAILED"
    retryable: ClassVar[bool] = False


class EmbeddingConfigError(EmbeddingError):
    """Misconfiguration or exhausted billing — no retry will fix it."""

    code: ClassVar[str] = "EMBEDDING_CONFIG"


class EmbeddingAuthError(EmbeddingError):
    """The API key was rejected (401) or lacks permission (403)."""

    code: ClassVar[str] = "EMBEDDING_AUTH"


class EmbeddingRejectedError(EmbeddingError):
    """The provider rejected the request itself (400/422)."""

    code: ClassVar[str] = "EMBEDDING_REJECTED"


class EmbeddingInputError(EmbeddingError):
    """A text failed the local pre-flight — empty, or longer than the input ceiling.

    Raised before any request is issued. For a chunk this is an invariant breach (the
    chunker guarantees `target + min <= EMBEDDING_MAX_INPUT_CHARS`); for a query it is
    user input, which is why T-206 renders it as a 400 rather than a 500.
    """

    code: ClassVar[str] = "EMBEDDING_INPUT_INVALID"


class EmbeddingDimensionError(EmbeddingError):
    """The model returned vectors the `document_chunks.embedding` column cannot hold."""

    code: ClassVar[str] = "EMBEDDING_DIMENSION_MISMATCH"


class EmbeddingResponseError(EmbeddingError):
    """The response did not line up with the request — wrong count, or a bad index."""

    code: ClassVar[str] = "EMBEDDING_RESPONSE_INVALID"


class EmbeddingRateLimitedError(EmbeddingError):
    """Throttled (429) beyond the SDK's own retry budget."""

    code: ClassVar[str] = "EMBEDDING_RATE_LIMITED"
    retryable: ClassVar[bool] = True


class EmbeddingUnavailableError(EmbeddingError):
    """The provider was unreachable or failed server-side (5xx, timeout, connection)."""

    code: ClassVar[str] = "EMBEDDING_UNAVAILABLE"
    retryable: ClassVar[bool] = True


def _error_body_code(exc: Exception) -> str:
    """The provider's `error.code` from an SDK exception body, or ``""``.

    The SDK unwraps the envelope before storing it — `_make_status_error_from_response`
    keeps ``payload["error"]`` when present — so `body` is normally the inner object
    already. Both shapes are read here so a change on either side degrades to "unknown
    code" rather than to a misclassified error.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        body = error
    code = body.get("code")
    return code if isinstance(code, str) else ""


def _translate(exc: Exception) -> EmbeddingError:
    """Map an SDK exception onto the taxonomy above.

    Imported lazily so the module stays cheap to import and testable without the SDK
    on the path.
    """
    import openai

    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return EmbeddingUnavailableError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIResponseValidationError):
        return EmbeddingResponseError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        # A 429 is *usually* throttling, but it is also how exhausted credit is reported.
        # Classifying that as retryable makes every queued document burn its full arq
        # backoff budget before dead-lettering — hours of a stalled pipeline reporting a
        # transient condition that will never clear.
        if _error_body_code(exc) in _TERMINAL_429_CODES:
            return EmbeddingConfigError(f"OpenAI quota exhausted: {exc}")
        return EmbeddingRateLimitedError(str(exc))
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return EmbeddingAuthError(str(exc))
    if isinstance(exc, openai.NotFoundError):
        return EmbeddingConfigError(f"unknown embedding model or endpoint: {exc}")
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        return EmbeddingRejectedError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 409:
            return EmbeddingUnavailableError(str(exc))
        return EmbeddingRejectedError(str(exc))
    # Anything unrecognised is terminal on purpose. Retrying a failure nobody classified
    # is spend without a hypothesis; T-208's retry endpoint puts a human in that loop.
    return EmbeddingError(f"{type(exc).__name__}: {exc}")


# --- batching -----------------------------------------------------------------


def plan_batches(
    texts: Sequence[str], *, max_batch_size: int, max_batch_chars: int
) -> list[tuple[int, ...]]:
    """Group indices into API requests. Pure — unit-tested with no client.

    Bounded by request count and by total characters, **never by `token_count`** (R-35(7)):
    that field is a `len/4` estimate, and an underestimate is a hard 400 mid-ingestion.
    Characters are charged at one token each, the same pessimistic rule
    :data:`~app.config.EMBEDDING_MAX_INPUT_CHARS` already uses — using one charging rule in
    both places is what makes the two constants provably consistent.

    A text longer than ``max_batch_chars`` still gets its own batch rather than looping
    forever; :func:`_validate_inputs` and the `EmbeddingSettings` validator between them
    make that branch unreachable, but a planner that can hang is not worth the saving.
    """
    batches: list[tuple[int, ...]] = []
    current: list[int] = []
    chars = 0
    for index, text in enumerate(texts):
        size = len(text)
        if current and (len(current) >= max_batch_size or chars + size > max_batch_chars):
            batches.append(tuple(current))
            current, chars = [], 0
        current.append(index)
        chars += size
    if current:
        batches.append(tuple(current))
    return batches


def _validate_inputs(texts: Sequence[str]) -> None:
    """Reject empty and over-long texts before a single request is issued."""
    for index, text in enumerate(texts):
        if not text.strip():
            raise EmbeddingInputError(f"text at index {index} is empty")
        if len(text) > EMBEDDING_MAX_INPUT_CHARS:
            raise EmbeddingInputError(
                f"text at index {index} is {len(text):,} characters; "
                f"the ceiling is {EMBEDDING_MAX_INPUT_CHARS:,}"
            )


def _check_dimensions(vector: Sequence[float], *, model: str) -> None:
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingDimensionError(
            f"model {model!r} returned {len(vector)}-dimensional vectors, but "
            f"document_chunks.embedding is VECTOR({EMBEDDING_DIM}) "
            "(app.db.base.EMBEDDING_DIM)"
        )


# --- protocol -----------------------------------------------------------------


@runtime_checkable
class EmbeddingClient(Protocol):
    """The seam ingestion (T-205/T-207) and retrieval (T-206/T-305) both depend on."""

    @property
    def model(self) -> str:
        """The **configured** model id — the environment default, not what any call used.

        Since T-612 this is a fallback rather than the truth: an operator override
        (`ModelSlot.EMBEDDING`) is resolved per job and passed to the call below, so a caller
        that needs to know which model produced a vector must use the id **it passed**, never
        read it back off the client. `workers/ingest.py` does exactly that, and the
        FR-ING-03 fingerprint depends on it.
        """
        ...

    @property
    def dimensions(self) -> int: ...

    async def embed_texts(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        """Embed a corpus batch. Order-preserving: ``result[i]`` embeds ``texts[i]``.

        ``model`` overrides the configured id for this call only, mirroring what R-83 did to
        `ChatClient` — one pooled client, the id chosen per call. `on_startup` caches a single
        embedding client precisely so a job does not build a connection pool of its own, so
        the override has to travel on the call rather than on a new client.
        """
        ...

    async def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        """Embed one search query (T-206). Never batched, never deduped."""
        ...

    async def embed_queries(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        """Embed a turn's probes in one round trip (T-305). Order-preserving.

        Distinct from :meth:`embed_texts` in **budget, not in shape**: R-45(3) makes a chat
        turn embed up to four probes at once, and they belong on the query timeout and
        retry count (`EMBEDDING_QUERY_*`) — the corpus path can afford to ride out a
        rate-limit blip before a user is waiting, and this one cannot.
        """
        ...

    async def aclose(self) -> None:
        """Release any pooled connections."""
        ...


# --- OpenAI backend -----------------------------------------------------------


class OpenAIEmbeddingClient:
    """`text-embedding-3-*` over the official async SDK (R-15, §10.1).

    Retry and timeout are configured **on the SDK**, matching the botocore precedent in
    `S3ObjectStorage._get_client`, and for a stronger reason than consistency: the SDK
    honours `Retry-After` / `retry-after-ms`, backs off exponentially with jitter, and
    retries only 408/409/429/5xx and connection errors — never 400/401/403/404/422. A
    hand-rolled loop that ignores `Retry-After` during a rate-limit incident makes the
    incident worse, and FR-ING-04's coarse retry layer already exists in arq (T-207).

    What must be overridden is the timeout: the SDK's default is 600 seconds, which inside
    an ingestion worker is a hang rather than a timeout.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._api_key = settings.openai.api_key
        self._model = settings.openai.embedding_model
        embedding = settings.embedding
        self._max_batch_size = min(embedding.max_batch_size, _MAX_INPUTS_PER_REQUEST)
        self._max_batch_chars = embedding.max_batch_chars
        self._max_concurrent = embedding.max_concurrent_requests
        self._timeout = embedding.timeout_seconds
        self._query_timeout = embedding.query_timeout_seconds
        self._connect_timeout = embedding.connect_timeout_seconds
        self._max_retries = embedding.max_retries
        self._query_max_retries = embedding.query_max_retries
        self._client = None
        self._query_client = None
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIM

    # -- client lifecycle

    async def _get_clients(self):  # noqa: ANN202 — the SDK's client type is imported lazily
        if self._client is not None:
            return self._client, self._query_client
        async with self._lock:
            if self._client is None:
                if not self._api_key:
                    # Never fall back to the fake backend here. Silently embedding a
                    # corpus with nonsense vectors and reporting ACTIVE is far worse than
                    # refusing to start.
                    raise EmbeddingConfigError(
                        "OPENAI_API_KEY is empty; set it or select EMBEDDING_BACKEND=fake"
                    )
                from openai import AsyncOpenAI

                client = AsyncOpenAI(
                    api_key=self._api_key,
                    max_retries=self._max_retries,
                    timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout),
                )
                # A copy shares the underlying pooled httpx client, so the chat path gets
                # its own budget without a second connection pool.
                self._query_client = client.with_options(
                    max_retries=self._query_max_retries,
                    timeout=httpx.Timeout(self._query_timeout, connect=self._connect_timeout),
                )
                self._client = client
        return self._client, self._query_client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._query_client = None

    # -- requests

    async def _request(  # noqa: ANN001 — the SDK's client type is imported lazily
        self, client, texts: Sequence[str], *, model: str
    ) -> list[list[float]]:
        """One `embeddings.create` call, unpacked by `index` and dimension-checked.

        ``model`` is required rather than defaulted: every caller has already resolved which
        id it means, and a default here would let one path silently fall back to the
        configured value while its fingerprint claimed the override (T-612).
        """
        try:
            response = await client.embeddings.create(input=list(texts), model=model)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        if len(response.data) != len(texts):
            raise EmbeddingResponseError(
                f"requested {len(texts)} embeddings, received {len(response.data)}"
            )

        # Never zip(texts, response.data). The API returns an `index` precisely because
        # order is not contractual, and a transposed corpus is undetectable downstream:
        # every vector is individually valid and every chunk is wrong.
        slots: list[list[float] | None] = [None] * len(texts)
        for item in response.data:
            if not 0 <= item.index < len(texts):
                raise EmbeddingResponseError(f"response index {item.index} is out of range")
            if slots[item.index] is not None:
                raise EmbeddingResponseError(f"response index {item.index} was returned twice")
            _check_dimensions(item.embedding, model=model)
            slots[item.index] = list(item.embedding)
        if any(slot is None for slot in slots):
            raise EmbeddingResponseError("response did not cover every requested index")
        return [slot for slot in slots if slot is not None]

    async def embed_texts(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        _validate_inputs(texts)
        model = model or self._model
        client, _ = await self._get_clients()

        batches = plan_batches(
            texts,
            max_batch_size=self._max_batch_size,
            max_batch_chars=self._max_batch_chars,
        )
        results: list[list[float] | None] = [None] * len(texts)
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def run(batch: tuple[int, ...]) -> None:
            async with semaphore:
                vectors = await self._request(client, [texts[i] for i in batch], model=model)
            for position, index in enumerate(batch):
                results[index] = vectors[position]

        try:
            # TaskGroup rather than gather: gather leaves siblings running after the first
            # failure, so you pay for embeddings you then discard.
            async with asyncio.TaskGroup() as group:
                for batch in batches:
                    group.create_task(run(batch))
        except* EmbeddingError as group:
            raise group.exceptions[0] from None

        log.info(
            "embeddings.batch_complete",
            model=model,
            inputs=len(texts),
            requests=len(batches),
        )
        if any(vector is None for vector in results):
            raise EmbeddingResponseError("not every input received an embedding")
        return [vector for vector in results if vector is not None]

    async def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        _validate_inputs([text])
        _, query_client = await self._get_clients()
        vectors = await self._request(query_client, [text], model=model or self._model)
        return vectors[0]

    async def embed_queries(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        _validate_inputs(texts)
        _, query_client = await self._get_clients()
        # One request, not one per probe. `_request` already unpacks by the response's own
        # `index` rather than by position (T-205), so alignment survives a reordered reply.
        # Unbatched on purpose: the caller's probe count is bounded by
        # `ROUTER_MAX_SUB_QUERIES + 1` and each probe by `ROUTER_MAX_PROBE_CHARS`, which is
        # three orders of magnitude inside the array and token ceilings `embed_texts` batches
        # against — a batching loop here would be dead code guarding an unreachable input.
        return await self._request(query_client, texts, model=model or self._model)


# --- fake backend -------------------------------------------------------------


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """A stable pseudo-random unit vector for ``text``.

    Determinism is load-bearing rather than convenient: identical text must produce an
    identical vector, or the fake cannot exercise the FR-ING-03 fingerprint diff end to
    end. L2-normalised so cosine distance stays meaningful for T-206's fusion tests.
    Standard library only — `numpy` was dropped from the lock by R-34(7).
    """
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


class FakeEmbeddingClient:
    """In-process deterministic embeddings for dev and CI (`EMBEDDING_BACKEND=fake`).

    Sanctioned in the spirit of R-19's filesystem object-storage backend and
    `QUEUE_BACKEND=none`. Records what it was asked to embed so tests can assert that an
    unchanged document cost zero inputs — the whole claim of FR-ING-03.

    **It also records the model each call named** (`models_used`), which is what lets a test
    assert T-612's central invariant offline: the id written into `document_chunks.metadata`
    is the id that produced the vector. Vectors stay derived from the text alone, so a flip
    does not perturb any existing fixture — the fake cannot simulate two vector spaces, and
    is not asked to. What it can prove is provenance, which is the part that must not drift.
    """

    def __init__(self, model: str = "fake-embedding", *, dimensions: int = EMBEDDING_DIM) -> None:
        self._model = model
        self._dimensions = dimensions
        self.embedded_inputs = 0
        self.request_count = 0
        self.models_used: list[str] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_texts(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        _validate_inputs(texts)
        self.embedded_inputs += len(texts)
        self.request_count += 1
        self.models_used.append(model or self._model)
        return [_deterministic_vector(text, self._dimensions) for text in texts]

    async def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        _validate_inputs([text])
        self.embedded_inputs += 1
        self.request_count += 1
        self.models_used.append(model or self._model)
        return _deterministic_vector(text, self._dimensions)

    async def embed_queries(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        _validate_inputs(texts)
        self.embedded_inputs += len(texts)
        self.request_count += 1
        self.models_used.append(model or self._model)
        return [_deterministic_vector(text, self._dimensions) for text in texts]

    async def aclose(self) -> None:
        return None


# --- factory / DI -------------------------------------------------------------

_client: EmbeddingClient | None = None


def build_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    """Construct the backend named by ``EMBEDDING_BACKEND`` (no caching).

    Selection is explicit and never inferred from a missing API key — see
    `OpenAIEmbeddingClient._get_clients`.
    """
    settings = settings or get_settings()
    if settings.embedding.backend == "fake":
        return FakeEmbeddingClient(settings.openai.embedding_model)
    return OpenAIEmbeddingClient(settings)


def get_embedding_client() -> EmbeddingClient:
    """Process-wide embedding client — also usable as a FastAPI dependency.

    Cached because the SDK pools connections in a client bound to the running event loop;
    :func:`close_embedding_client` (app lifespan / test teardown) tears it down.
    """
    global _client
    if _client is None:
        _client = build_embedding_client()
    return _client


async def close_embedding_client() -> None:
    """Close and forget the cached instance."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


#: Route dependency for the retrieval surface (T-206/T-305), following the `Annotated` DI
#: convention established in `app.auth.dependencies`. There is deliberately **no**
#: readiness probe for this client: NFR-REL-02's dependency set is database / broker /
#: object storage / worker, and probing OpenAI on every poll would both cost money and
#: make our own liveness depend on a third party's.
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client)]
