"""The NFR-CAP-01 conversation budget — FR-STA-04's block and FR-ANL-03's meter (T-310).

**langgraph-free**, for the `errors.py` / `generation.py` / `groundedness.py` /
`evaluation.py` reason: `app.rag.graph` calls `apply_strict_msgpack()` at import time, and
the API routes that enforce this must reach it without inheriting that.

**What is counted is settled by R-30: conversation history + the current query, nothing
else.** The system prompt and the retrieved chunks are excluded and bounded separately by
`RERANK_TOP_K`. One consequence is worth stating plainly because it looks like a defect
until you see it: **the model's real `prompt_tokens` will routinely exceed this meter, and
that is correct.** The API reports a single number covering system prompt + chunks + history
+ query; R-30 counts two of those four. So `messages.prompt_tokens` — which T-402 persists
and the FR-ANL telemetry reads — measures *cost*, and this module measures *conversation
length*. They are different quantities and neither can be derived from the other.

**Usage is derived, never stored**, and that is what makes two open questions answer
themselves rather than needing rules of their own:

* **FR-MSG-08 Regenerate** replaces `messages.content` in place, so the recomputed total
  reflects the answer that now exists. A stored counter would have to be decremented by the
  old answer and incremented by the new one, and would drift the first time that was missed.
* **FR-RET-03 decomposition** sub-queries are probes (R-45(3)); they never become `messages`
  rows, so they cannot consume a user's budget. Both were `# TBD(§8.4)` and both close here
  by construction.

The cost of deriving it is bounded by the thing itself: a conversation at the limit is
~10.4K tokens of text, so the read this does is small *because* the budget exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories.messages import MessageRepository
from app.tokens import estimate_tokens

__all__ = [
    "ContextUsage",
    "SubmissionCheck",
    "check_regeneration",
    "check_submission",
    "conversation_usage",
]


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """What FR-ANL-03's CONTEXT WINDOW card renders, for one conversation.

    `used_tokens` is conversation length per R-30 — every `messages.content` in this chat,
    user turns and assistant replies alike (R-30(1) names both). Per-chat, so a new chat
    starts at zero (OI-16, resolved Rev 0.6.1).

    **`answer_reserve_tokens` lives here rather than being re-read from settings by each
    check, and that is the point of it being a field.** T-513 publishes it on
    `ContextWindowResponse` so the browser can reproduce :func:`check_submission` before a
    request exists (R-51(2)/(3), §8.56(4)) — and holding it on the same object the checks
    read makes *the number the client is told* provably *the number the server decided with*.
    Two readers of one setting is how those quietly become two numbers.

    **No default.** A default is exactly how a caller silently disagrees with the operator's
    configuration; the value is floored at `LLM_MAX_OUTPUT_TOKENS` by `Settings._coherent`,
    so a plausible-looking literal here would be wrong in a way nothing would report.
    """

    used_tokens: int
    limit_tokens: int
    answer_reserve_tokens: int

    @property
    def remaining_tokens(self) -> int:
        """Never negative: the card shows a full bar, not a debt."""
        return max(0, self.limit_tokens - self.used_tokens)

    @property
    def percent_used(self) -> float:
        """0–100, clamped. Drives the FR-ANL-03 progress fill and its `{pct}% used` caption."""
        if self.limit_tokens <= 0:
            return 100.0
        return min(100.0, round(self.used_tokens * 100.0 / self.limit_tokens, 1))


@dataclass(frozen=True, slots=True)
class SubmissionCheck:
    """FR-STA-04's decision for one attempted message."""

    allowed: bool
    projected_tokens: int
    query_tokens: int
    usage: ContextUsage

    @property
    def overflow_tokens(self) -> int:
        """How far over the limit the projection lands — 0 when allowed."""
        return max(0, self.projected_tokens - self.usage.limit_tokens)


async def conversation_usage(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> ContextUsage:
    """Current NFR-CAP-01 usage for one conversation, computed from `messages`.

    Counts **per message and sums**, rather than counting one concatenated string:
    `sum(ceil(len_i / 4))` and `ceil(sum(len_i) / 4)` are different numbers, and only the
    per-message form is one the GUI can reproduce for a lone composer box.

    An unknown conversation reads as empty rather than raising — the caller has already
    authorized it, and a 404 is that caller's decision to make, not this function's.
    """
    settings = settings or get_settings()
    contents = await MessageRepository(session).list_contents(conversation_id)
    used = sum(estimate_tokens(content) for content in contents)
    return ContextUsage(
        used_tokens=used,
        limit_tokens=settings.context.window_tokens,
        answer_reserve_tokens=settings.context.answer_reserve_tokens,
    )


def check_submission(usage: ContextUsage, query: str) -> SubmissionCheck:
    """Decide FR-STA-04 for `query` against `usage`. Pure — no I/O, no clock.

    The projection is `used + query + answer reserve`. The reserve is what makes the promise
    hold: the reply is part of the conversation the instant it lands, so without it a chat
    just under the limit accepts a query, answers it, and is over budget with no block ever
    having fired (see `ContextSettings.answer_reserve_tokens`).

    **The comparison is `>`, not `>=`** — a projection landing exactly on the limit is inside
    it. Ties are permissive, on R-44's standing rule that a control which refuses legitimate
    use is worse than no control, and the same disposition R-49(4) took at the gate.

    The reserve is read off `usage`, not off settings — see :class:`ContextUsage`. That is
    what makes this function take no `Settings` at all and be pure in the literal sense.
    """
    query_tokens = estimate_tokens(query)
    projected = usage.used_tokens + query_tokens + usage.answer_reserve_tokens
    return SubmissionCheck(
        allowed=projected <= usage.limit_tokens,
        projected_tokens=projected,
        query_tokens=query_tokens,
        usage=usage,
    )


def check_regeneration(usage: ContextUsage, replaced: str) -> SubmissionCheck:
    """Decide FR-STA-04 for an FR-MSG-08 Regenerate. Pure — no I/O, no clock (T-404).

    :func:`check_submission` is the wrong instrument here and would double-count twice over.
    A regenerate adds **no new question** — the user's turn is already a row — and the answer
    it is about to write **replaces** one that `usage` has already counted. So the projection
    is `used − tokens(replaced) + reserve`: the same shape as a send, with the outgoing answer
    subtracted instead of a new query added.

    Using the plain submission check instead would refuse a chat that is merely *full*, which
    is precisely the chat a user reaches for Regenerate in — a control that fails exactly when
    it is needed is the false-positive failure R-44 calls worse than no control.

    **The check carries the real `usage`, not the adjusted figure.** The `409` body feeds the
    FR-ANL-03 card, and a card showing a number the meter never displays would read as a bug in
    the meter. Only `projected_tokens` reflects the subtraction.

    A refusal here is still possible and still meaningful: a chat near the ceiling whose last
    answer is shorter than the reserve genuinely has no room for a new one. Ties are permissive,
    matching :func:`check_submission`.
    """
    replaced_tokens = estimate_tokens(replaced)
    # Floored at zero: `usage` is read in a separate statement from the row, so a concurrent
    # delete could in principle make the subtraction exceed the total. A negative projection
    # would silently allow anything.
    projected = max(0, usage.used_tokens - replaced_tokens) + usage.answer_reserve_tokens
    return SubmissionCheck(
        allowed=projected <= usage.limit_tokens,
        projected_tokens=projected,
        query_tokens=0,
        usage=usage,
    )
