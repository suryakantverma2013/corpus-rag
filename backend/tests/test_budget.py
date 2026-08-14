"""The NFR-CAP-01 conversation budget — FR-STA-04 and FR-ANL-03 (T-310, R-30).

The assertions that matter here are the ones pinning **what is not counted**. R-30's whole
content is an exclusion — retrieved chunk text and the system prompt stay out of the meter —
and the failure mode is silent: a meter that quietly included them would still show a
plausible percentage, and the first symptom would be users blocked out of chats that look
short. So `test_retrieval_volume_cannot_consume_a_users_budget` and
`test_the_meter_ignores_real_api_token_columns` are the load-bearing pair.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ContextSettings, LlmSettings, Settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.users import UserRepository
from app.rag.budget import (
    ContextUsage,
    check_regeneration,
    check_submission,
    conversation_usage,
)
from app.tokens import estimate_tokens

# 400 characters -> 100 tokens under the shared rule, so arithmetic in these tests is exact.
_100_TOKENS = "x" * 400


def _settings(**context: object) -> Settings:
    return Settings(context=ContextSettings(**context))  # type: ignore[arg-type]


async def _conversation(session: AsyncSession) -> Conversation:
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )
    return await ConversationRepository(session).add(
        Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="chat")
    )


async def _say(
    session: AsyncSession,
    conversation: Conversation,
    role: MessageRole,
    content: str,
    **columns: object,
) -> Message:
    message = await MessageRepository(session).add(
        Message(conversation_id=conversation.id, role=role, content=content, **columns)
    )
    await session.flush()
    return message


# --- the counting rule --------------------------------------------------------


def test_one_rule_shared_with_ingestion() -> None:
    """`app.tokens` is the single definition; the chunker's is an alias over it (T-310).

    Two copies of characters-per-token is how the number a user is shown stops matching the
    number that blocked them — and the GUI has to apply the same one pre-submission.
    """
    from app.ingestion.chunker import estimate_token_count

    for text in ("", "x", "hello world", _100_TOKENS):
        assert estimate_token_count(text) == estimate_tokens(text)


def test_counting_rounds_up_per_message_not_over_the_transcript() -> None:
    """`sum(ceil(len/4))` and `ceil(sum(len)/4)` differ; the per-message form is the contract.

    Only the per-message form is one the GUI can reproduce for a lone composer box without
    knowing the rest of the chat.
    """
    parts = ["a", "b", "c"]  # 1 char each -> 1 token each
    assert sum(estimate_tokens(p) for p in parts) == 3
    assert estimate_tokens("".join(parts)) == 1


# --- what the meter counts ----------------------------------------------------


async def test_a_new_chat_starts_at_zero(session: AsyncSession) -> None:
    """OI-16, resolved Rev 0.6.1: accounting is per chat."""
    conversation = await _conversation(session)
    usage = await conversation_usage(session, conversation.id, settings=_settings())
    assert usage.used_tokens == 0
    assert usage.percent_used == 0.0


async def test_the_meter_counts_both_sides_of_the_conversation(session: AsyncSession) -> None:
    """R-30(1) names it exactly: the user's own messages **plus** the assistant's replies."""
    conversation = await _conversation(session)
    await _say(session, conversation, MessageRole.USER, _100_TOKENS)
    await _say(session, conversation, MessageRole.AI, _100_TOKENS)

    usage = await conversation_usage(session, conversation.id, settings=_settings())
    assert usage.used_tokens == 200


async def test_the_meter_rounds_per_message_not_over_the_joined_transcript(
    session: AsyncSession,
) -> None:
    """The per-message rule, pinned where it is actually applied.

    Deliberately uses lengths that are **not** multiples of the divisor: with tidy 400-char
    messages both rules agree, so a meter that concatenated the transcript would pass every
    other test in this file. Three one-character messages are 3 tokens per-message and 1
    joined — and the GUI, which can only see the composer box, computes the former.
    """
    conversation = await _conversation(session)
    for _ in range(3):
        await _say(session, conversation, MessageRole.USER, "x")

    usage = await conversation_usage(session, conversation.id, settings=_settings())
    assert usage.used_tokens == 3


async def test_usage_is_per_chat(session: AsyncSession) -> None:
    """One user's second conversation does not inherit the first's length."""
    first = await _conversation(session)
    await _say(session, first, MessageRole.USER, _100_TOKENS)
    second = await _conversation(session)

    assert (await conversation_usage(session, first.id, settings=_settings())).used_tokens == 100
    assert (await conversation_usage(session, second.id, settings=_settings())).used_tokens == 0


async def test_the_meter_ignores_real_api_token_columns(session: AsyncSession) -> None:
    """R-30's exclusion, pinned against the number it is most likely to be replaced by.

    `messages.prompt_tokens` is the API's count of system prompt + retrieved chunks + history
    + query. R-30 counts two of those four, so the two quantities are unrelated and the meter
    must not drift toward the one that happens to be sitting in the same row.
    """
    conversation = await _conversation(session)
    await _say(
        session,
        conversation,
        MessageRole.AI,
        _100_TOKENS,
        prompt_tokens=99_000,
        completion_tokens=88_000,
    )

    usage = await conversation_usage(session, conversation.id, settings=_settings())
    assert usage.used_tokens == 100


async def test_retrieval_volume_cannot_consume_a_users_budget(session: AsyncSession) -> None:
    """R-30(3): a retrieval-heavy turn must not shorten the chat.

    The answer's own text counts; the passages it was grounded in do not, however many there
    were. Expressed here as the invariant that matters — two turns whose answers are the same
    length cost the same, whatever was retrieved to produce them.
    """
    conversation = await _conversation(session)
    await _say(session, conversation, MessageRole.AI, _100_TOKENS, citations={"source_ids": []})
    lean = (await conversation_usage(session, conversation.id, settings=_settings())).used_tokens

    heavy_conversation = await _conversation(session)
    await _say(
        session,
        heavy_conversation,
        MessageRole.AI,
        _100_TOKENS,
        citations={"source_ids": [str(uuid.uuid4()) for _ in range(50)]},
    )
    heavy = (
        await conversation_usage(session, heavy_conversation.id, settings=_settings())
    ).used_tokens

    assert lean == heavy == 100


async def test_regenerate_replaces_rather_than_accumulates(session: AsyncSession) -> None:
    """FR-MSG-08, closing a `# TBD(§8.4)` by construction rather than by a rule.

    Deriving usage from `messages` means a regenerated answer is simply re-measured. A stored
    counter would need decrementing by the old text and incrementing by the new, and would
    drift the first time that was missed.
    """
    conversation = await _conversation(session)
    answer = await _say(session, conversation, MessageRole.AI, _100_TOKENS)
    assert (
        await conversation_usage(session, conversation.id, settings=_settings())
    ).used_tokens == 100

    answer.content = _100_TOKENS * 2  # the FR-MSG-08 in-place replacement
    await session.flush()

    usage = await conversation_usage(session, conversation.id, settings=_settings())
    assert usage.used_tokens == 200, "a regenerated answer is re-measured, not added"


async def test_an_unknown_conversation_reads_as_empty(session: AsyncSession) -> None:
    """Authorization is the caller's decision; this function does not raise for it."""
    usage = await conversation_usage(session, uuid.uuid4(), settings=_settings())
    assert usage.used_tokens == 0


# --- FR-STA-04's decision -----------------------------------------------------


def test_the_reserve_is_what_keeps_the_transcript_under_budget() -> None:
    """The case that motivated reserving headroom at all.

    Counting `used + query` alone, this submission is comfortably inside the limit — and the
    answer then carries the conversation past it, with the block firing only on the *next*
    turn, after the budget was already breached.
    """
    usage = ContextUsage(used_tokens=9_000, limit_tokens=10_400, answer_reserve_tokens=1_500)

    check = check_submission(usage, "short question")

    assert check.projected_tokens == 9_000 + check.query_tokens + 1_500
    assert not check.allowed
    # Without the reserve the same submission would have been waved through.
    assert usage.used_tokens + check.query_tokens < usage.limit_tokens


def test_a_projection_landing_exactly_on_the_limit_is_allowed() -> None:
    """Ties are permissive — R-44's rule, and R-49(4)'s disposition at the gate."""
    usage = ContextUsage(used_tokens=8_400, limit_tokens=10_000, answer_reserve_tokens=1_500)

    check = check_submission(usage, _100_TOKENS)

    assert check.projected_tokens == 10_000
    assert check.allowed
    assert check.overflow_tokens == 0


def test_a_normal_turn_in_a_fresh_chat_is_allowed() -> None:
    """The reserve must not make the *first* message of a chat unsendable."""
    context = _settings().context
    usage = ContextUsage(
        used_tokens=0,
        limit_tokens=context.window_tokens,
        answer_reserve_tokens=context.answer_reserve_tokens,
    )
    assert check_submission(usage, "What is the refund window?").allowed


def test_overflow_reports_how_far_over_the_projection_lands() -> None:
    usage = ContextUsage(used_tokens=480, limit_tokens=2_000, answer_reserve_tokens=1_500)

    check = check_submission(usage, _100_TOKENS)

    assert not check.allowed
    assert check.projected_tokens == 480 + 100 + 1_500
    assert check.overflow_tokens == 80


# --- what FR-ANL-03 renders ---------------------------------------------------


def test_the_card_never_shows_a_negative_remainder_or_over_100_percent() -> None:
    """A conversation can legitimately sit over budget — the reserve bounds the *next* turn,
    and a long answer can still land above the line. The card shows a full bar, not a debt."""
    usage = ContextUsage(used_tokens=12_000, limit_tokens=10_400, answer_reserve_tokens=1_500)
    assert usage.remaining_tokens == 0
    assert usage.percent_used == 100.0


def test_percent_used_matches_the_progress_fill() -> None:
    def at(used: int) -> ContextUsage:
        return ContextUsage(used_tokens=used, limit_tokens=10_400, answer_reserve_tokens=1_500)

    assert at(3_800).percent_used == 36.5
    assert at(0).percent_used == 0.0


# --- settings guards ----------------------------------------------------------


def test_a_reserve_below_the_answer_ceiling_is_refused_at_boot() -> None:
    """The cross-group validator: an answer may run to `LLM_MAX_OUTPUT_TOKENS`, so a smaller
    reserve promises a guarantee it cannot keep — and would fail one long answer at a time."""
    with pytest.raises(ValueError, match="CONTEXT_ANSWER_RESERVE_TOKENS"):
        Settings(
            context=ContextSettings(answer_reserve_tokens=100),
            llm=LlmSettings(max_output_tokens=1_500),
        )


def test_a_reserve_that_swallows_the_window_is_refused() -> None:
    """Otherwise every submission is blocked, including the first message of a new chat."""
    with pytest.raises(ValueError, match="CONTEXT_ANSWER_RESERVE_TOKENS"):
        ContextSettings(window_tokens=1_000, answer_reserve_tokens=1_000)


def test_there_is_no_context_enabled_switch() -> None:
    """Its off state would remove FR-STA-04 rather than degrade it — the `GATE_ENABLED` test.

    Contrast `EVAL_ENABLED`, which is legitimate because FR-EVL-01 says a response *may*
    carry scores.
    """
    assert "enabled" not in ContextSettings.model_fields


# --- the reserve is one number, not two (T-513) --------------------------------


async def test_usage_carries_the_configured_reserve(session: AsyncSession) -> None:
    """`ContextWindowResponse` publishes this so the GUI can reproduce `check_submission`.

    It has to come from the same read as the limit, or the operator can raise one and not the
    other and nothing reports it.
    """
    conversation = await _conversation(session)
    settings = _settings(window_tokens=10_400, answer_reserve_tokens=2_000)

    usage = await conversation_usage(session, conversation.id, settings=settings)

    assert usage.answer_reserve_tokens == 2_000
    assert usage.limit_tokens == 10_400


def test_the_checks_read_the_reserve_off_usage_not_off_settings() -> None:
    """The guarantee the published field exists for, stated as a test.

    Both projections must move when `usage.answer_reserve_tokens` moves and there is no
    `Settings` in sight — that is what makes the number the client is told the number the
    server decided with (R-51(2)). If either function reached for `get_settings()` instead,
    these two would agree and the wire would be lying.
    """
    lean = ContextUsage(used_tokens=8_000, limit_tokens=10_000, answer_reserve_tokens=1_000)
    fat = ContextUsage(used_tokens=8_000, limit_tokens=10_000, answer_reserve_tokens=2_000)

    assert check_submission(lean, _100_TOKENS).allowed
    assert not check_submission(fat, _100_TOKENS).allowed
    assert (
        check_submission(fat, _100_TOKENS).projected_tokens
        - check_submission(lean, _100_TOKENS).projected_tokens
        == 1_000
    )

    assert (
        check_regeneration(fat, _100_TOKENS).projected_tokens
        - check_regeneration(lean, _100_TOKENS).projected_tokens
        == 1_000
    )


def test_the_reserve_has_no_default() -> None:
    """A default is how a caller silently disagrees with the operator's configuration.

    The value is floored at `LLM_MAX_OUTPUT_TOKENS` by `Settings._coherent`, so a plausible
    literal here would be wrong in a way nothing would report.
    """
    with pytest.raises(TypeError):
        ContextUsage(used_tokens=0, limit_tokens=10_400)  # type: ignore[call-arg]


# --- FR-MSG-08 Regenerate's own projection (T-404) ----------------------------


def test_a_full_chat_can_still_regenerate_its_last_answer() -> None:
    """The test that *is* the guard: plain `check_submission` refuses this and must not be used.

    A regenerate adds **no new question** — the user's turn is already a row — and the answer it
    is about to write **replaces** one `usage` has already counted. Projecting it as a
    submission therefore double-counts and refuses precisely the chat a user reaches for
    Regenerate in: the full one. A control that fails exactly when it is needed is the
    false-positive failure R-44 calls worse than no control.
    """
    usage = ContextUsage(used_tokens=10_000, limit_tokens=10_400, answer_reserve_tokens=1_500)
    replaced = "x" * 8_000  # 2,000 tokens under the shared rule

    check = check_regeneration(usage, replaced)

    assert check.allowed
    assert check.projected_tokens == 10_000 - 2_000 + 1_500
    assert not check_submission(usage, "").allowed, (
        "the submission projection is what this exists to replace"
    )


def test_a_regenerate_is_still_refused_when_the_replacement_cannot_fit() -> None:
    """The refusal is real, not theoretical: a near-ceiling chat whose last answer is shorter
    than the reserve genuinely has no room for a new one."""
    usage = ContextUsage(used_tokens=10_300, limit_tokens=10_400, answer_reserve_tokens=1_500)

    check = check_regeneration(usage, "x" * 40)  # 10 tokens

    assert not check.allowed
    assert check.overflow_tokens > 0


def test_the_regenerate_check_reports_the_real_usage_not_the_adjusted_one() -> None:
    """The `409` body feeds the FR-ANL-03 card, and a card showing a number the meter never
    displays would read as a bug in the meter."""
    usage = ContextUsage(used_tokens=20_000, limit_tokens=10_400, answer_reserve_tokens=1_500)

    check = check_regeneration(usage, _100_TOKENS)

    assert check.usage.used_tokens == 20_000
    assert check.projected_tokens == 20_000 - 100 + 1_500
    assert check.query_tokens == 0, "a regenerate adds no query"


def test_a_regenerate_projection_landing_exactly_on_the_limit_is_allowed() -> None:
    """Ties permissive, matching `check_submission` — the same rule, not a second one."""
    usage = ContextUsage(used_tokens=8_600, limit_tokens=10_000, answer_reserve_tokens=1_500)

    check = check_regeneration(usage, _100_TOKENS)

    assert check.projected_tokens == 10_000
    assert check.allowed
