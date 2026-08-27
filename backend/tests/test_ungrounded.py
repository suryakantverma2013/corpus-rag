"""FR-MSG-09 — the optional ungrounded answer (R-98, T-727).

Three properties carry this feature, and two of them fail **silently** if they regress: an
answer that quietly cites, and an answer that quietly re-enters the prompt. Nothing else in
the suite would notice either, which is why they are asserted here rather than left to the
route tests.

The third — that the deployment switch is off by default and cannot itself produce an answer —
is what lets `docs/LIMITATIONS.md` still say there is no setting that turns grounding off.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.messages import MessageRepository
from app.rag.history import load_history
from app.services import ungrounded

pytestmark = pytest.mark.usefixtures("patch_jwks")


async def _chat(session: AsyncSession, make_token: Callable[..., str]) -> Conversation:
    from app.db.repositories.users import UserRepository

    sub = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=sub, email=f"{sub.hex[:8]}@corpus.local", display_name="U"
    )
    row = Conversation(owner_id=sub, title="T-727")
    session.add(row)
    await session.commit()
    return row


async def _abstention(session: AsyncSession, chat: Conversation) -> Message:
    """A question and the refusal it drew. The refusal cites nothing — that is what makes it one."""
    repo = MessageRepository(session)
    await repo.add(
        Message(conversation_id=chat.id, role=MessageRole.USER, content="Explain induction.")
    )
    target = await repo.add(
        Message(
            conversation_id=chat.id,
            role=MessageRole.AI,
            content="I couldn't ground an answer to that in your documents",
            citations=None,
        )
    )
    await session.commit()
    return target


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Turn the control on for the tests that need it, leaving the default alone elsewhere."""
    settings = get_settings()
    monkeypatch.setattr(settings.ungrounded, "fallback_enabled", True)
    return settings


async def test_the_switch_is_off_by_default(session: AsyncSession) -> None:
    """R-98(2). The default is the argument, not an accident.

    Abstentions are diagnostic: three of them against a calculus textbook are how B-007's
    corrupt text layer was found, and a deployment that has not thought about this should
    behave exactly as it did before R-98.
    """
    from app.config import UngroundedSettings

    assert UngroundedSettings.model_fields["fallback_enabled"].default is False


async def test_it_refuses_when_the_deployment_has_not_enabled_it(
    session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The switch gates the *service*, not merely the GUI control.

    If it only hid the button, an enabled client — or a direct call — could still produce
    ungrounded text in a deployment that never opted in.
    """
    chat = await _chat(session, make_token)
    target = await _abstention(session, chat)

    with pytest.raises(ungrounded.UngroundedDisabledError):
        await ungrounded.answer_from_general_knowledge(
            session, conversation=chat, target=target
        )


async def test_the_answer_cites_nothing_and_is_marked(
    session: AsyncSession, make_token: Callable[..., str], enabled
) -> None:
    """R-98(3)/(5): the two properties that would fail silently.

    `citations` is empty **by construction** — no passages were supplied, so no `[S<n>]` can
    resolve — and `ungrounded` is what every later history read filters on. A regression in
    either produces a perfectly normal-looking answer.
    """
    chat = await _chat(session, make_token)
    target = await _abstention(session, chat)

    row = await ungrounded.answer_from_general_knowledge(
        session, conversation=chat, target=target
    )

    assert row.ungrounded is True
    assert not row.citations
    assert row.content
    assert row.id != target.id, "it appends; it must not overwrite the abstention"


async def test_the_abstention_survives_beneath_it(
    session: AsyncSession, make_token: Callable[..., str], enabled
) -> None:
    """R-98(1): the abstention is the record that the corpus could not answer.

    Replacing it would delete the evidence the whole ruling leans on — the reason an automatic
    fallback is declined is that abstentions are how a broken corpus becomes visible.
    """
    chat = await _chat(session, make_token)
    target = await _abstention(session, chat)

    await ungrounded.answer_from_general_knowledge(session, conversation=chat, target=target)

    await session.refresh(target)
    assert target.ungrounded is False
    assert "couldn't ground" in target.content


async def test_it_is_withheld_from_the_history_a_later_turn_sees(
    session: AsyncSession, make_token: Callable[..., str], enabled
) -> None:
    """R-98(5), and the one that matters most.

    An invented claim re-entering as trusted `assistant` speech could ground a later answer —
    OI-32's hazard with "possibly poisoned" upgraded to "fabricated by construction". The
    filter lives in the repository queries, so this asserts the *observable* consequence rather
    than the SQL: ask for the history a subsequent turn would be given, and the ungrounded
    answer is not in it.
    """
    chat = await _chat(session, make_token)
    target = await _abstention(session, chat)
    row = await ungrounded.answer_from_general_knowledge(
        session, conversation=chat, target=target
    )

    # A later question, as a real turn would write it.
    later = await MessageRepository(session).add(
        Message(conversation_id=chat.id, role=MessageRole.USER, content="Explain that further.")
    )
    await session.commit()

    history = await load_history(session, chat.id, until_message_id=later.id)
    contents = [turn.content for turn in history]

    assert row.content not in contents, "an ungrounded answer must never become context"
    assert "couldn't ground" in " ".join(contents), "the abstention itself still belongs there"


async def test_a_grounded_answer_is_not_a_fallback_target(
    session: AsyncSession, make_token: Callable[..., str], enabled
) -> None:
    """The control is offered on an abstention only (FR-MSG-09).

    Checked against the stored row rather than a flag the client sends: "this turn abstained"
    is a fact about the transcript, and a client that claimed otherwise would otherwise get a
    second, ungrounded answer beside a perfectly good cited one.
    """
    chat = await _chat(session, make_token)
    repo = MessageRepository(session)
    await repo.add(Message(conversation_id=chat.id, role=MessageRole.USER, content="q"))
    answered = await repo.add(
        Message(
            conversation_id=chat.id,
            role=MessageRole.AI,
            content="Grounded.",
            citations={"segments": [{"isCite": True, "doc": "a.pdf", "quote": "q"}]},
        )
    )
    await session.commit()

    with pytest.raises(ungrounded.NotAnAbstentionError):
        await ungrounded.answer_from_general_knowledge(
            session, conversation=chat, target=answered
        )


async def test_the_offer_predicate_agrees_with_the_service(
    session: AsyncSession, make_token: Callable[..., str], enabled
) -> None:
    """One predicate, so the GUI and the route cannot disagree about what is offerable.

    R-71(1) had to reconcile exactly this kind of drift between a client-side signal and a
    server refusal; here the two read the same function.
    """
    chat = await _chat(session, make_token)
    target = await _abstention(session, chat)
    assert ungrounded.is_offerable(target) is True

    row = await ungrounded.answer_from_general_knowledge(
        session, conversation=chat, target=target
    )
    assert ungrounded.is_offerable(row) is False, "no fallback on a fallback"
