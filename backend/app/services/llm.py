"""Chat-completions client seam (T-304, R-45(1), R-15).

One :class:`ChatClient` protocol, two backends: the official OpenAI SDK and a deterministic
in-process fake for dev and CI. The shape deliberately mirrors
:mod:`app.services.embeddings` — same error taxonomy, same lazy SDK import, same
`build_/get_/close_` trio, same per-call-site timeout budgets — because it is the same kind
of object: a pooled HTTP client bound to the running event loop and released in the app
lifespan.

**One method per call site, each with its own budget** — the way `embed_texts` and
`embed_query` share one client and split their patience. `complete_json` is T-304's router
call; `rerank_json` is T-306's FR-RET-02 scoring call, on a different model and a longer
leash; `stream_answer` is T-307's generation call, on `OPENAI_CHAT_MODEL` and the most
patient budget of the three. The budget is applied per request through the SDK's own
`with_options`, so all of them share one pooled connection.

`stream_answer` is the only method that streams, and it streams for a **server-side** reason
(R-48(1)): NFR-PRF-02 puts the FR-CIT-06 gate between generation and the client, so the
answer is buffered here regardless. What streaming buys is that a provider which stops
sending mid-answer is detected at the next read rather than at the full `LLM_TIMEOUT_SECONDS`,
and that T-402 — and NFR-PRF-02's own **(D)** incremental-verification escape — inherit an
iterator instead of needing a second seam.

Why `services/` and not `rag/`: `app/rag/prompts.py`, `app/rag/router.py` and
`app/rag/errors.py` are pure and import-light on purpose, and the graph consumes this
through :class:`app.rag.state.RAGContext` like every other injected dependency.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
import structlog

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

__all__ = [
    "AnswerStream",
    "ChatAuthError",
    "ChatClient",
    "ChatConfigError",
    "ChatError",
    "ChatJson",
    "ChatRateLimitedError",
    "ChatRefusedError",
    "ChatRejectedError",
    "ChatResponseError",
    "ChatText",
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


@dataclass(frozen=True, slots=True)
class ChatText:
    """A completed free-text answer plus the metering the turn records (T-307)."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _Delta:
    """One event from a backend's stream. Module-private: the backend↔stream contract.

    Modelled as a value rather than as attributes a backend writes onto the stream, so that
    all the accumulation and every terminal check live in :class:`AnswerStream` and are
    tested once — a backend only has to say what arrived.
    """

    text: str = ""
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    refusal: str | None = None


class AnswerStream:
    """A generation in flight: iterate for text deltas, or :meth:`collect` for the whole answer.

    T-307's node calls `collect()`, because NFR-PRF-02 puts the FR-CIT-06 gate between
    generation and the client — nothing may be served until the answer is complete and
    verified. The iterator is what T-402 and NFR-PRF-02's **(D)** would consume later; it
    exists now so adopting either is a routing change rather than a change to this seam.

    **Single-pass.** ``__aiter__`` hands back the *same* underlying generator every time, so
    `collect()` after a partial iteration returns what was actually received rather than
    silently restarting a stream the provider has already closed.
    """

    __slots__ = (
        "_deltas",
        "_iterator",
        "_parts",
        "_refusal",
        "completion_tokens",
        "finish_reason",
        "model",
        "prompt_tokens",
    )

    def __init__(self, deltas: AsyncIterator[_Delta], *, model: str) -> None:
        self._deltas = deltas
        self._iterator: AsyncIterator[str] | None = None
        self._parts: list[str] = []
        self._refusal: list[str] = []
        self.model = model
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.finish_reason: str | None = None

    @property
    def text(self) -> str:
        """Everything received so far. Complete only once the stream is exhausted."""
        return "".join(self._parts)

    def __aiter__(self) -> AsyncIterator[str]:
        if self._iterator is None:
            self._iterator = self._iterate()
        return self._iterator

    async def _iterate(self) -> AsyncIterator[str]:
        async for delta in self._deltas:
            if delta.model:
                self.model = delta.model
            if delta.prompt_tokens is not None:
                self.prompt_tokens = delta.prompt_tokens
            if delta.completion_tokens is not None:
                self.completion_tokens = delta.completion_tokens
            if delta.finish_reason:
                self.finish_reason = delta.finish_reason
            if delta.refusal:
                self._refusal.append(delta.refusal)
            if delta.text:
                self._parts.append(delta.text)
                yield delta.text

    async def collect(self) -> ChatText:
        """Drain the stream and return the whole answer, or raise.

        The three terminal checks are the streaming form of the ones `_complete_json` makes
        after a single response, and they are made **here** rather than in each backend so a
        refusal reads the same whichever backend produced it:

        * a refusal is a :class:`ChatRefusedError`, not an empty answer — R-45's reason for
          giving it its own class is that "the model declined" and "the model answered and we
          could not read it" call for different handling, and only the first is worth showing
          a user as anything but a system failure;
        * ``finish_reason == "length"`` is a truncated answer, and a truncated *grounded*
          answer is worse than none: it can drop the citation for a claim it already made,
          which FR-CIT-06 would then reject anyway. The operator's fix is a number
          (`LLM_MAX_OUTPUT_TOKENS`), so it is distinguished from a generic failure;
        * an empty answer is a failure rather than an abstention. R-23's abstention is a
          *decision* the graph makes with copy of its own; silence from the provider is not
          that, and dressing it up as one would report an outage as a grounded answer.
        """
        async for _ in self:
            pass
        refusal = "".join(self._refusal).strip()
        if refusal:
            raise ChatRefusedError(refusal)
        if self.finish_reason == "length":
            raise ChatResponseError("generation hit the token ceiling and was truncated")
        text = self.text
        if not text.strip():
            raise ChatResponseError("generation returned empty content")
        return ChatText(
            text=text,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


# --- protocol -----------------------------------------------------------------


@runtime_checkable
class ChatClient(Protocol):
    """The seam the router (T-304), the reranker (T-306) and generation (T-307) depend on."""

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        """One schema-constrained completion on `OPENAI_ROUTER_MODEL`, on the router budget.

        ``LLM_ROUTER_TIMEOUT_SECONDS`` / ``LLM_ROUTER_MAX_RETRIES`` — a short leash, because
        the caller sits before retrieval on the chat critical path. A second call site with
        different patience adds a *method*, as `embed_texts` does beside `embed_query`; it
        must not add a budget parameter here, or the leash becomes something every caller
        has to remember.

        Raises a :class:`ChatError` subclass on every failure, including a refusal and a
        payload that does not parse as an object.
        """
        ...

    async def rerank_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        """One schema-constrained completion on `OPENAI_RERANK_MODEL`, on the rerank budget.

        ``LLM_RERANK_TIMEOUT_SECONDS`` / ``LLM_RERANK_MAX_RETRIES`` (T-306, R-47). A separate
        method rather than a parameter for the reason above, and a separate *model* because
        ranking quality and classification accuracy are tuned against different evidence.

        Raises the same taxonomy as :meth:`complete_json`. `app.rag.rerank` catches all of
        it — R-47(2) makes the reranker fail open — but the seam still reports honestly.
        """
        ...

    async def evaluate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        """One schema-constrained completion on `OPENAI_JUDGE_MODEL`, on the eval budget.

        ``LLM_EVAL_TIMEOUT_SECONDS`` / ``LLM_EVAL_MAX_RETRIES`` (T-309, R-50). A fourth
        method rather than a parameter, for the reason given above — and the budget is the
        most patient of the four *because* nothing waits on it: this call site runs in the
        worker, after the answer has been served, so latency costs a chip appearing a few
        seconds later rather than a user staring at an empty bubble.

        This is the seam DeepEval's judge is driven through. Routing its LLM calls here
        rather than letting it open its own client is what keeps `LLM_BACKEND=fake` honest,
        keeps the error taxonomy ours, and keeps one key in one place.

        Raises the same taxonomy as :meth:`complete_json`. `app.rag.evaluation` catches all
        of it — R-50 makes evaluation fail open — but the seam still reports honestly.
        """
        ...

    async def stream_answer(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
    ) -> AnswerStream:
        """FR-SYS-02 grounded generation on `OPENAI_CHAT_MODEL` (T-307, R-48).

        ``LLM_TIMEOUT_SECONDS`` / ``LLM_MAX_RETRIES`` — the most patient of the three
        budgets, and **never** the router's `LLM_ROUTER_*` leash: an 8-second cap tuned for a
        classification that fails open would cut off a long grounded answer that fails closed.

        Free text, not a schema: an answer interleaves prose with `[S<n>]` markers, and
        constraining that to JSON would put the model's whole composition inside a string
        field for no gain. Returns before any token has been read — the request is issued and
        the caller drives it.

        Raises the same taxonomy as :meth:`complete_json`, at call time or from the iterator.
        Unlike the other two call sites, nothing catches it: R-48(2) makes generation fail
        closed.
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

    One pooled client, several budgets. The client is constructed with generation's budget
    (T-307's, the most patient) and each call site narrows it per request through the SDK's
    `with_options`, which returns a shallow copy sharing the same connection pool. Baking one
    call site's leash into the constructor — as this class did while the router was its only
    caller — would force a second client, a second pool and a second lifespan hook for every
    method added.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self._api_key = settings.openai.api_key
        self._router_model = settings.openai.router_model
        self._rerank_model = settings.openai.rerank_model
        self._chat_model = settings.openai.chat_model
        self._judge_model = settings.openai.judge_model
        llm = settings.llm
        self._timeout = llm.timeout_seconds
        self._max_retries = llm.max_retries
        self._router_timeout = llm.router_timeout_seconds
        self._router_max_retries = llm.router_max_retries
        self._rerank_timeout = llm.rerank_timeout_seconds
        self._rerank_max_retries = llm.rerank_max_retries
        self._eval_timeout = llm.eval_timeout_seconds
        self._eval_max_retries = llm.eval_max_retries
        self._connect_timeout = llm.connect_timeout_seconds
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def model(self) -> str:
        """The model :meth:`complete_json` calls."""
        return self._router_model

    @property
    def rerank_model(self) -> str:
        """The model :meth:`rerank_json` calls."""
        return self._rerank_model

    @property
    def chat_model(self) -> str:
        """The model :meth:`stream_answer` calls — FR-SYS-03's configurable answer model."""
        return self._chat_model

    @property
    def judge_model(self) -> str:
        """The model :meth:`evaluate_json` calls — the FR-EVL-01 judge (R-15)."""
        return self._judge_model

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
                    max_retries=self._max_retries,
                    timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout),
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
        return await self._complete_json(
            messages,
            model=self._router_model,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            timeout_seconds=self._router_timeout,
            max_retries=self._router_max_retries,
        )

    async def rerank_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        return await self._complete_json(
            messages,
            model=self._rerank_model,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            timeout_seconds=self._rerank_timeout,
            max_retries=self._rerank_max_retries,
        )

    async def evaluate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        return await self._complete_json(
            messages,
            model=self._judge_model,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            timeout_seconds=self._eval_timeout,
            max_retries=self._eval_max_retries,
        )

    async def stream_answer(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
    ) -> AnswerStream:
        client = (await self._get_client()).with_options(
            timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout),
            max_retries=self._max_retries,
        )
        try:
            stream = await client.chat.completions.create(
                model=self._chat_model,
                messages=[dict(message) for message in messages],  # type: ignore[arg-type]
                max_completion_tokens=max_output_tokens,
                stream=True,
                # Without this the SDK streams no `usage` at all, and `prompt_tokens` /
                # `completion_tokens` — FR-MSG-06 fields, the FR-ANL cards and OI-16's
                # per-chat accounting — would be silently `None` on every real answer while
                # every mocked test that supplies them stayed green.
                stream_options={"include_usage": True},
                # No `temperature`, for the reason spelled out in `_complete_json`. It
                # matters more here, not less: this call site fails *closed*, so a newer
                # model family rejecting the parameter would fail the turn outright.
            )
        except ChatError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc
        return AnswerStream(self._deltas(stream), model=self._chat_model)

    @staticmethod
    async def _deltas(stream: Any) -> AsyncIterator[_Delta]:
        """Translate SDK chunks into :class:`_Delta`s, translating failures as they arrive.

        The `try` wraps the **iteration**, not just the request. A stream that dies at token
        fifty raises from `__anext__`, and an untranslated `openai.APIConnectionError` there
        would still classify as `LLM_ERROR` through `classify`'s `OpenAIError` fall-through —
        but a *timeout* would land on `TIMEOUT` rather than `LLM_ERROR`, and a rate limit mid
        stream would miss `RATE_LIMITED`'s "wait a moment" copy entirely. Translating here
        keeps one taxonomy for both halves of the call.
        """
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                choices = getattr(chunk, "choices", None) or ()
                if not choices:
                    # The final usage-only chunk `stream_options` asks for.
                    if usage is not None:
                        yield _Delta(
                            model=getattr(chunk, "model", None),
                            prompt_tokens=getattr(usage, "prompt_tokens", None),
                            completion_tokens=getattr(usage, "completion_tokens", None),
                        )
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                yield _Delta(
                    text=getattr(delta, "content", None) or "",
                    model=getattr(chunk, "model", None),
                    prompt_tokens=getattr(usage, "prompt_tokens", None),
                    completion_tokens=getattr(usage, "completion_tokens", None),
                    finish_reason=getattr(choice, "finish_reason", None),
                    refusal=getattr(delta, "refusal", None) or "",
                )
        except ChatError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

    async def _complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
        timeout_seconds: float,
        max_retries: int,
    ) -> ChatJson:
        """One structured completion. Every call site's budget is applied here, per request."""
        client = (await self._get_client()).with_options(
            timeout=httpx.Timeout(timeout_seconds, connect=self._connect_timeout),
            max_retries=max_retries,
        )
        try:
            response = await client.chat.completions.create(
                model=model,
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

        # No `temperature`, and that is a decision rather than an omission: output under
        # `strict` schema constraints barely varies, while newer model families reject any
        # value but the default — and that rejection is a 400, which both call sites turn
        # into a silent fail-open (R-45(2) to `hybrid`, R-47(2) to the retrieval order). A
        # parameter that can disable a whole feature when someone changes a model id is not
        # worth the determinism it buys.
        if not response.choices:
            raise ChatResponseError("chat completion returned no choices")
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ChatRefusedError(str(choice.message.refusal))
        if choice.finish_reason == "length":
            # Distinguished from a generic parse failure because the operator's fix is a
            # number (`ROUTER_MAX_OUTPUT_TOKENS` / `RERANK_MAX_OUTPUT_TOKENS`), not an
            # investigation.
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
            model=response.model or model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )


# --- fake backend -------------------------------------------------------------

#: The schema names this fake knows how to answer. Kept as literals rather than imported
#: from `app.rag.router` / `app.rag.rerank` so the seam does not depend on its own consumers.
_ROUTER_SCHEMA_NAME = "query_route"
_RERANK_SCHEMA_NAME = "passage_relevance"

#: Matches the `[S<n>]` markers `app.rag.rerank` puts in front of each candidate.
_SOURCE_MARKER = re.compile(r"\[S(\d+)\]")


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


def _fake_rerank(messages: Sequence[Mapping[str, str]], scale: int) -> dict[str, Any]:
    """A deterministic stand-in relevance scoring for `LLM_BACKEND=fake` (T-306).

    **A test double's convenience, never an implementation of FR-RET-02.** What it buys is a
    dev box and a CI run where the batching, the fail-open path, the score alignment and the
    top-K truncation are all exercised without an API key — the `_fake_route` bargain.

    Scores by the fraction of the query's words the passage contains, which is enough to make
    the fake's ordering *differ* from the retrieval order (a double that returned a constant
    would let a broken sort pass every test) while staying a pure function of the payload.
    """
    query = _unfence(messages[-1]["content"]) if messages else ""
    context = messages[-2]["content"] if len(messages) >= 2 else ""
    terms = {word for word in re.findall(r"\w+", query.lower()) if len(word) > 2}

    scores: list[dict[str, int]] = []
    parts = _SOURCE_MARKER.split(context)
    # `re.split` on one capturing group yields [prologue, id, body, id, body, ...].
    for index in range(1, len(parts) - 1, 2):
        marker = int(parts[index])
        body = set(re.findall(r"\w+", parts[index + 1].lower()))
        overlap = len(terms & body) / len(terms) if terms else 0.0
        scores.append({"id": marker, "score": round(overlap * scale)})
    return {"scores": scores}


def _fake_evaluate(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A deterministic stand-in judge payload for `LLM_BACKEND=fake` (T-309).

    **A test double's convenience, never an implementation of FR-EVL-01** — the `_fake_route`
    bargain a fourth time. What it buys is a dev box and a CI run where the adapter, the
    worker, the skip branches, the fail-open path and the payload shape are all exercised
    without an API key.

    Built from the **JSON schema the caller supplied** rather than from a table of DeepEval's
    internal model names (`Claims`, `Verdicts`, `Statements`, …). Those names are a vendor
    internal: pinning the double to them would make a library upgrade fail here, in a fake,
    with a message about a missing branch rather than about the upgrade. Synthesising from
    the schema keeps the double honest against whatever the judge actually asks for.

    Every verdict it emits is affirmative, so both metrics score 1.0. A test that needs a
    *low* score passes `handler=` — a double that could be coaxed into interesting numbers
    by construction would be an implementation, which is exactly what this is not.
    """
    return {
        name: _fake_value(spec, schema) for name, spec in (schema.get("properties") or {}).items()
    }


def _fake_value(spec: Mapping[str, Any], root: Mapping[str, Any]) -> Any:
    """One minimal value satisfying `spec`, for :func:`_fake_evaluate`.

    `root` is threaded through so `$ref` can be resolved. Pydantic lifts every nested model
    into `$defs` and refers to it, so without this a list of verdict *objects* is described
    by a spec carrying no `type` at all — and a double that guessed "string" there produced
    a validation error deep inside the vendor, which is a confusing way to learn that the
    fake is wrong.
    """
    spec = _deref(spec, root)
    if enum := spec.get("enum"):
        return enum[0]
    if options := spec.get("anyOf") or spec.get("oneOf"):
        # Optional[X] is `anyOf: [X, null]`; the first non-null branch is the useful one.
        concrete = [o for o in options if isinstance(o, dict) and o.get("type") != "null"]
        if concrete:
            return _fake_value(concrete[0], root)
        return None
    match spec.get("type"):
        case "array":
            return [_fake_value(spec.get("items") or {}, root)]
        case "object":
            return {n: _fake_value(s, root) for n, s in (spec.get("properties") or {}).items()}
        case "integer":
            return 1
        case "number":
            return 1.0
        case "boolean":
            return True
        case _:
            # Strings. DeepEval reads a verdict as affirmative unless it is literally "no",
            # so this is the affirmative branch for a verdict field and harmless prose for a
            # reason field.
            return "yes"


def _deref(spec: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve a local `$ref` against the schema root; other refs are left alone."""
    ref = spec.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return spec
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, Mapping) or part not in node:
            return spec
        node = node[part]
    return node if isinstance(node, Mapping) else spec


def _fake_generate(messages: Sequence[Mapping[str, str]]) -> list[str]:
    """A deterministic stand-in answer for `LLM_BACKEND=fake` (T-307).

    **A test double's convenience, never an implementation of FR-SYS-02** — the `_fake_route`
    bargain again. What it buys is a dev box and a CI run where the prompt composition, the
    streaming buffer, the `[S<n>]` segmentation and the graph wiring are all exercised without
    an API key.

    It **must** emit markers, and it must emit only markers the fenced context actually
    carried: a double that answered in plain prose would let a broken segment parser pass
    every test, and one that invented `[S9]` would make R-48(6)'s drop rule look like the
    normal path. Returned as several parts so the caller streams a marker across delta
    boundaries, which is the arrangement that breaks a parser written against whole chunks.
    """
    context = next(
        (message["content"] for message in messages if message["content"].startswith("<<<")),
        "",
    )
    markers = sorted({int(index) for index in _SOURCE_MARKER.findall(context)})
    if not markers:
        return ["I can't ground an answer to that from the documents supplied."]
    query = _unfence(messages[-1]["content"]) if messages else ""
    parts = [f"Grounded answer to: {query}".strip()]
    for marker in markers[:3]:
        parts.extend([" Supported by source ", f"[S{marker}]", "."])
    return parts


class FakeChatClient:
    """In-process deterministic completions for dev and CI (`LLM_BACKEND=fake`).

    Sanctioned in the spirit of R-19's filesystem object storage and `QUEUE_BACKEND=none`.
    Records every call so a test can assert the router was reached — or, for the kill switch,
    that it was not.

    ``handler`` overrides the default decision; ``error`` makes every call raise; ``answer``
    fixes exactly what :meth:`stream_answer` streams. They exist so `tests/test_router.py`,
    `tests/test_rerank.py` and `tests/test_generation.py` can drive the fail-open, fail-closed
    and validation paths without patching the SDK.
    """

    def __init__(
        self,
        *,
        model: str = "fake-chat",
        handler: Callable[[Sequence[Mapping[str, str]]], Mapping[str, Any]] | None = None,
        error: Exception | None = None,
        raw: str | None = None,
        answer: str | None = None,
        score_scale: int = 10,
    ) -> None:
        self._model = model
        self._handler = handler
        self._error = error
        self._raw = raw
        self._answer = answer
        self._score_scale = score_scale
        self.calls: list[list[dict[str, str]]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def rerank_model(self) -> str:
        return self._model

    @property
    def chat_model(self) -> str:
        return self._model

    @property
    def judge_model(self) -> str:
        return self._model

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        return self._answer_json(messages, schema_name)

    async def rerank_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        return self._answer_json(messages, schema_name)

    async def evaluate_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ChatJson:
        return self._answer_json(messages, schema_name, schema=schema)

    async def stream_answer(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
    ) -> AnswerStream:
        self.calls.append([dict(message) for message in messages])
        if self._error is not None:
            raise self._error
        parts = [self._answer] if self._answer is not None else _fake_generate(messages)
        # Metering is stubbed with something non-zero rather than left `None`: a caller that
        # dropped the usage plumbing would otherwise look identical to a working one here,
        # and that is precisely the field a mocked test cannot vouch for.
        deltas = [_Delta(text=part) for part in parts]
        deltas.append(
            _Delta(
                prompt_tokens=sum(len(m["content"]) for m in messages) // 4,
                completion_tokens=sum(len(part) for part in parts) // 4,
            )
        )

        async def produce() -> AsyncIterator[_Delta]:
            for delta in deltas:
                yield delta

        return AnswerStream(produce(), model=self._model)

    def _answer_json(
        self,
        messages: Sequence[Mapping[str, str]],
        schema_name: str,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> ChatJson:
        """Shared by all three JSON methods, so `handler`/`error`/`raw` drive any call site."""
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
        if schema_name == _RERANK_SCHEMA_NAME:
            return ChatJson(_fake_rerank(messages, self._score_scale), model=self._model)
        if schema is not None:
            # Only `evaluate_json` supplies the schema, and its `schema_name` is a vendor
            # internal we deliberately do not enumerate — see `_fake_evaluate`.
            return ChatJson(_fake_evaluate(schema), model=self._model)
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
        return FakeChatClient(
            model=settings.openai.router_model,
            score_scale=settings.rerank.score_scale,
        )
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
