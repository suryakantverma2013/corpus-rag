"""Chat-completions client seam (T-304, R-45(1), R-15).

One :class:`ChatClient` protocol, two backends: the official OpenAI SDK and a deterministic
in-process fake for dev and CI. The shape deliberately mirrors
:mod:`app.services.embeddings` — same error taxonomy, same lazy SDK import, same
`build_/get_/close_` trio, same per-call-site timeout budgets — because it is the same kind
of object: a pooled HTTP client bound to the running event loop and released in the app
lifespan.

**Scope is exactly what T-304 uses.** Only structured JSON completion ships here.
T-307's streaming generation adds its own method with its own budget (the way
`embed_texts` and `embed_query` share one client and split their patience), rather than
this task guessing at a streaming API nothing yet exercises.

Why `services/` and not `rag/`: `app/rag/prompts.py`, `app/rag/router.py` and
`app/rag/errors.py` are pure and import-light on purpose, and the graph consumes this
through :class:`app.rag.state.RAGContext` like every other injected dependency.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
import structlog

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

__all__ = [
    "ChatAuthError",
    "ChatClient",
    "ChatConfigError",
    "ChatError",
    "ChatJson",
    "ChatRateLimitedError",
    "ChatRefusedError",
    "ChatRejectedError",
    "ChatResponseError",
    "ChatUnavailableError",
    "FakeChatClient",
    "OpenAIChatClient",
    "build_chat_client",
    "close_chat_client",
    "get_chat_client",
]


# --- errors -------------------------------------------------------------------
#
# `code` is the contract `app.rag.errors.classify` reads (it maps by `exc.code`, not by
# exception type) — so every code below has an entry in that module's `_CODE_TO_CLASS`.
# Without one, a translated SDK failure would classify as SYSTEM_FAILURE, because
# translating it here is precisely what stops it being an `openai.OpenAIError` any more.


class ChatError(Exception):
    """A chat completion could not be completed."""

    code: ClassVar[str] = "CHAT_FAILED"
    retryable: ClassVar[bool] = False


class ChatConfigError(ChatError):
    """Misconfiguration or exhausted billing — no retry will fix it."""

    code: ClassVar[str] = "CHAT_CONFIG"


class ChatAuthError(ChatError):
    """The API key was rejected (401) or lacks permission (403)."""

    code: ClassVar[str] = "CHAT_AUTH"


class ChatRejectedError(ChatError):
    """The provider rejected the request itself (400/422)."""

    code: ClassVar[str] = "CHAT_REJECTED"


class ChatRefusedError(ChatError):
    """The model returned a refusal instead of content.

    A first-class outcome of structured outputs, not a transport failure — which is why it
    is not folded into :class:`ChatResponseError`. T-307 must be able to tell "the model
    declined" from "the model answered and we could not read it".
    """

    code: ClassVar[str] = "CHAT_REFUSED"


class ChatResponseError(ChatError):
    """The response was empty, truncated, or not the JSON the schema promised."""

    code: ClassVar[str] = "CHAT_RESPONSE_INVALID"


class ChatRateLimitedError(ChatError):
    """Throttled (429) beyond the SDK's own retry budget."""

    code: ClassVar[str] = "CHAT_RATE_LIMITED"
    retryable: ClassVar[bool] = True


class ChatUnavailableError(ChatError):
    """The provider was unreachable or failed server-side (5xx, timeout, connection)."""

    code: ClassVar[str] = "CHAT_UNAVAILABLE"
    retryable: ClassVar[bool] = True


#: 429 bodies carrying one of these `error.code` values are exhausted billing, not
#: throttling; they never clear on their own. Same list as `app.services.embeddings`.
_TERMINAL_429_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


def _error_body_code(exc: Exception) -> str:
    """The provider's `error.code` from an SDK exception body, or ``""``."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        body = error
    code = body.get("code")
    return code if isinstance(code, str) else ""


def _translate(exc: Exception) -> ChatError:
    """Map an SDK exception onto the taxonomy above (lazy import, as in `embeddings`)."""
    import openai

    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return ChatUnavailableError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIResponseValidationError):
        return ChatResponseError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        if _error_body_code(exc) in _TERMINAL_429_CODES:
            return ChatConfigError(f"OpenAI quota exhausted: {exc}")
        return ChatRateLimitedError(str(exc))
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ChatAuthError(str(exc))
    if isinstance(exc, openai.NotFoundError):
        return ChatConfigError(f"unknown chat model or endpoint: {exc}")
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        # Where an unsupported *parameter* lands. See `OpenAIChatClient._request` on why
        # this request sends as few of them as it can get away with.
        return ChatRejectedError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 409:
            return ChatUnavailableError(str(exc))
        return ChatRejectedError(str(exc))
    return ChatError(f"{type(exc).__name__}: {exc}")


# --- result -------------------------------------------------------------------


class ChatJson:
    """A parsed structured completion plus the metering the turn records.

    A plain class rather than a frozen dataclass with `slots` only because `data` is a
    mutable mapping either way — callers must treat it as read-only.
    """

    __slots__ = ("completion_tokens", "data", "model", "prompt_tokens")

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        self.data = dict(data)
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ChatJson(model={self.model!r}, keys={sorted(self.data)})"


# --- protocol -----------------------------------------------------------------


@runtime_checkable
class ChatClient(Protocol):
    """The seam the FR-RET-03 router (T-304) depends on, and T-307 will extend."""

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        """One schema-constrained completion, on the **router** budget.

        ``LLM_ROUTER_TIMEOUT_SECONDS`` / ``LLM_ROUTER_MAX_RETRIES`` — a short leash, because
        the only caller today sits before retrieval on the chat critical path. A second call
        site with different patience adds a *method*, as `embed_texts` does beside
        `embed_query`; it must not add a budget parameter here, or the leash becomes
        something every caller has to remember.

        Raises a :class:`ChatError` subclass on every failure, including a refusal and a
        payload that does not parse as an object.
        """
        ...

    async def aclose(self) -> None:
        """Release any pooled connections."""
        ...


# --- OpenAI backend -----------------------------------------------------------


class OpenAIChatClient:
    """Chat completions over the official async SDK (R-15, §10.1).

    Retry and timeout are configured **on the SDK**, for the reasons spelled out in
    `OpenAIEmbeddingClient`: it honours `Retry-After`, backs off with jitter, and retries
    only the status codes worth retrying. What must be overridden is the 600-second default
    timeout, which on a request path is a hang rather than a timeout.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._api_key = settings.openai.api_key
        self._router_model = settings.openai.router_model
        llm = settings.llm
        self._router_timeout = llm.router_timeout_seconds
        self._router_max_retries = llm.router_max_retries
        self._connect_timeout = llm.connect_timeout_seconds
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        """The model :meth:`complete_json` calls."""
        return self._router_model

    async def _get_client(self):  # noqa: ANN202 — the SDK's client type is imported lazily
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                if not self._api_key:
                    # Never fall back to the fake. A router that silently classified every
                    # query with a stub would be indistinguishable from a working one.
                    raise ChatConfigError(
                        "OPENAI_API_KEY is empty; set it or select LLM_BACKEND=fake"
                    )
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=self._api_key,
                    max_retries=self._router_max_retries,
                    timeout=httpx.Timeout(self._router_timeout, connect=self._connect_timeout),
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        client = await self._get_client()
        try:
            response = await client.chat.completions.create(
                model=self._router_model,
                messages=[dict(message) for message in messages],  # type: ignore[arg-type]
                max_completion_tokens=max_output_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": dict(schema)},
                },
            )
        except ChatError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        # No `temperature`, and that is a decision rather than an omission: a classification
        # under `strict` schema constraints barely varies, while newer model families reject
        # any value but the default — and that rejection is a 400, which R-45(2) turns into a
        # silent fail-open to `hybrid`. A parameter that can disable the whole feature when
        # someone changes OPENAI_ROUTER_MODEL is not worth the determinism it buys.
        if not response.choices:
            raise ChatResponseError("chat completion returned no choices")
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ChatRefusedError(str(choice.message.refusal))
        if choice.finish_reason == "length":
            # Distinguished from a generic parse failure because the operator's fix is a
            # number (`ROUTER_MAX_OUTPUT_TOKENS`), not an investigation.
            raise ChatResponseError(
                f"chat completion hit the {max_output_tokens}-token ceiling and was truncated"
            )
        content = choice.message.content or ""
        if not content.strip():
            raise ChatResponseError("chat completion returned empty content")
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise ChatResponseError(f"chat completion was not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ChatResponseError(
                f"chat completion JSON was {type(data).__name__}, not an object"
            )

        usage = response.usage
        return ChatJson(
            data,
            model=response.model or self._router_model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )


# --- fake backend -------------------------------------------------------------

#: The router schema name this fake knows how to answer. Kept as a literal rather than
#: imported from `app.rag.router` so the seam does not depend on its own consumer.
_ROUTER_SCHEMA_NAME = "query_route"


def _unfence(content: str) -> str:
    """The payload inside a fenced block, best-effort.

    Structural lines — the delimiters (``<<<…>>>``) and the ``key=`` labels — are dropped by
    *shape* rather than by matching the composer's exact layout, so this double does not
    break every time that copy is tuned. It is allowed to be approximate: being wrong here
    produces a differently-classified fake decision, never a wrong production decision.
    """
    lines = [
        line
        for line in content.splitlines()
        if line.strip() and not line.startswith("<<<") and not line.rstrip().endswith("=")
    ]
    return "\n".join(lines).strip()


def _fake_route(messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """A deterministic stand-in decision for `LLM_BACKEND=fake`.

    **This is a test double's convenience, never an implementation of FR-RET-03.** R-45(1)
    rules out in-tree classification for the real path precisely because the interesting
    branches are generative; what this buys is a local dev box and a CI run where the T-305
    fan-out, the probe caps and the graph wiring are all exercised without an API key.

    Keyed off the last message, which `compose_router_messages` guarantees carries the query.
    """
    query = _unfence(messages[-1]["content"]) if messages else ""
    parts = [part.strip() for part in query.replace("?", "?\n").split("\n") if part.strip()]

    if len(parts) > 1:
        return {"query_class": "multi_part", "probes": parts[:3]}
    if " and " in query.lower():
        halves = [half.strip() for half in query.split(" and ") if half.strip()]
        return {"query_class": "multi_part", "probes": halves[:3]}
    if len(query.split()) <= 3:
        return {"query_class": "vague", "probes": [f"{query} details"]}
    return {"query_class": "simple", "probes": []}


class FakeChatClient:
    """In-process deterministic completions for dev and CI (`LLM_BACKEND=fake`).

    Sanctioned in the spirit of R-19's filesystem object storage and `QUEUE_BACKEND=none`.
    Records every call so a test can assert the router was reached — or, for the kill switch,
    that it was not.

    ``handler`` overrides the default decision; ``error`` makes every call raise. Both exist
    so `tests/test_router.py` can drive the fail-open and validation paths without patching
    the SDK.
    """

    def __init__(
        self,
        *,
        model: str = "fake-chat",
        handler: Callable[[Sequence[Mapping[str, str]]], Mapping[str, Any]] | None = None,
        error: Exception | None = None,
        raw: str | None = None,
    ) -> None:
        self._model = model
        self._handler = handler
        self._error = error
        self._raw = raw
        self.calls: list[list[dict[str, str]]] = []

    @property
    def model(self) -> str:
        return self._model

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        self.calls.append([dict(message) for message in messages])
        if self._error is not None:
            raise self._error
        if self._raw is not None:
            # Exercises the "the model returned something unparseable" branch through the
            # same code path a real response takes.
            try:
                data = json.loads(self._raw)
            except ValueError as exc:
                raise ChatResponseError(str(exc)) from exc
            if not isinstance(data, dict):
                raise ChatResponseError("not an object")
            return ChatJson(data, model=self._model)
        if self._handler is not None:
            return ChatJson(self._handler(messages), model=self._model)
        if schema_name == _ROUTER_SCHEMA_NAME:
            return ChatJson(_fake_route(messages), model=self._model)
        return ChatJson({}, model=self._model)

    async def aclose(self) -> None:
        return None


# --- factory / DI -------------------------------------------------------------

_client: ChatClient | None = None


def build_chat_client(settings: Settings | None = None) -> ChatClient:
    """Construct the backend named by ``LLM_BACKEND`` (no caching).

    Selection is explicit and never inferred from a missing API key — see
    `OpenAIChatClient._get_client`.
    """
    settings = settings or get_settings()
    if settings.llm.backend == "fake":
        return FakeChatClient(model=settings.openai.router_model)
    return OpenAIChatClient(settings)


def get_chat_client() -> ChatClient:
    """Process-wide chat client. Cached because the SDK pools connections per event loop."""
    global _client
    if _client is None:
        _client = build_chat_client()
    return _client


async def close_chat_client() -> None:
    """Close and forget the cached instance (app lifespan / test teardown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
