"""Runtime model selection — resolution, overrides and the CLI (T-611, R-83).

The property under test throughout is that **environment is the default and a row is an
override**, so a deployment that never writes here is bit-for-bit unchanged, and that a
failure to read the overrides degrades to that default rather than to an error.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.config import get_settings
from app.db.repositories.model_overrides import ModelOverrideRepository
from app.services.model_selection import (
    ModelSelection,
    ModelSlot,
    UnknownModelSlotError,
    clear_model_override,
    parse_slot,
    resolve_models,
    set_model_override,
)

# --- resolution ---------------------------------------------------------------


async def test_an_empty_table_resolves_to_the_configured_defaults(session: AsyncSession) -> None:
    """The normal state of a deployment, and the one that must not change.

    Compared against the live settings rather than literals — a hard-coded `gpt-4o` here
    would still pass on the day someone changed the default, which is the drift this whole
    module exists to make visible rather than to reproduce.
    """
    openai = get_settings().openai

    selection = await resolve_models(session)

    assert selection == ModelSelection(
        chat=openai.chat_model,
        router=openai.router_model,
        rerank=openai.rerank_model,
        judge=openai.judge_model,
        judge_escalation=openai.judge_escalation_model,
    )


async def test_an_override_replaces_exactly_one_slot(session: AsyncSession) -> None:
    """The other four must not move. A resolver that rebuilt the whole selection from the
    first row it found would pass a single-slot assertion and fail this one."""
    openai = get_settings().openai
    await set_model_override(
        session, slot=ModelSlot.ROUTER, model_id="gpt-4o-mini-2030", updated_by="test"
    )

    selection = await resolve_models(session)

    assert selection.router == "gpt-4o-mini-2030"
    assert selection.chat == openai.chat_model
    assert selection.rerank == openai.rerank_model
    assert selection.judge == openai.judge_model
    assert selection.judge_escalation == openai.judge_escalation_model


async def test_every_slot_is_independently_reachable(session: AsyncSession) -> None:
    """Each slot resolves from its own row.

    Written as a loop over `ModelSlot` rather than five assertions so that adding a slot
    without wiring it into `resolve_models` fails here instead of shipping a knob that
    silently does nothing.
    """
    for slot in ModelSlot:
        await set_model_override(
            session, slot=slot, model_id=f"model-for-{slot.value}", updated_by="test"
        )

    selection = await resolve_models(session)

    for slot in ModelSlot:
        assert selection.for_slot(slot) == f"model-for-{slot.value}"


async def test_setting_the_same_slot_twice_keeps_one_row(session: AsyncSession) -> None:
    """Last writer wins. An insert without the upsert would raise on the second call."""
    await set_model_override(session, slot=ModelSlot.CHAT, model_id="first", updated_by="a")
    await set_model_override(session, slot=ModelSlot.CHAT, model_id="second", updated_by="b")

    rows = await ModelOverrideRepository(session).list_all()

    assert [(row.slot, row.model_id, row.updated_by) for row in rows] == [
        (ModelSlot.CHAT.value, "second", "b")
    ]
    assert (await resolve_models(session)).chat == "second"


async def test_an_unknown_slot_in_the_table_is_ignored(session: AsyncSession) -> None:
    """A row this version does not understand must not break the turn.

    Not defensive padding: `embedding` becomes a slot once T-608 lands, and a process rolled
    back to an older build has to keep answering while a newer one's row sits in the table.
    """
    await session.execute(
        text("INSERT INTO model_overrides (slot, model_id) VALUES ('embedding', 'whatever')")
    )

    selection = await resolve_models(session)

    assert selection.chat == get_settings().openai.chat_model


async def test_a_failed_read_fails_open_and_leaves_the_session_usable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The savepoint, and it is the assertion that matters most in this file.

    Postgres aborts the entire transaction on a failed statement, so catching the error
    without a savepoint returns the right answer and leaves the caller's session poisoned —
    every later statement raising `InFailedSQLTransactionError` a long way from the cause.
    The concrete trigger is code deployed ahead of `alembic upgrade head`.

    The second assertion is the whole test. Without it this passes against the broken
    version, because the *return value* is correct either way.
    """

    async def _boom(self: ModelOverrideRepository) -> None:
        await self.session.execute(text("SELECT * FROM a_table_that_does_not_exist"))

    monkeypatch.setattr(ModelOverrideRepository, "list_all", _boom)

    selection = await resolve_models(session)

    assert selection.chat == get_settings().openai.chat_model
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


# --- clearing -----------------------------------------------------------------


async def test_clearing_restores_the_environment_default(session: AsyncSession) -> None:
    await set_model_override(session, slot=ModelSlot.CHAT, model_id="temporary", updated_by=None)

    assert await clear_model_override(session, slot=ModelSlot.CHAT) is True
    assert (await resolve_models(session)).chat == get_settings().openai.chat_model


async def test_clearing_an_unset_slot_reports_that_nothing_was_cleared(
    session: AsyncSession,
) -> None:
    """The boolean is what lets the tool tell "reverted" from "you mistyped the slot"."""
    assert await clear_model_override(session, slot=ModelSlot.JUDGE) is False


# --- the slot vocabulary ------------------------------------------------------


def test_there_is_no_embedding_slot() -> None:
    """R-83(4), pinned so that adding one trips a test that explains why it must not.

    `OPENAI_EMBEDDING_MODEL` is an FR-ING-03 fingerprint input read by the chunker. Changing
    it at runtime leaves existing chunks holding model-A vectors while new ingests write
    model-B ones, and both are then compared in the same cosine query — silently wrong
    retrieval, not a degraded ranking, with nothing failing anywhere. T-608 owns the
    controlled re-embed that has to come first; until it exists this slot must not.
    """
    assert "embedding" not in {slot.value for slot in ModelSlot}
    assert not hasattr(ModelSelection("a", "b", "c", "d", "e"), "embedding")


def test_an_unknown_slot_name_names_the_legal_ones() -> None:
    """The caller is a human at a shell who has just mistyped one."""
    with pytest.raises(UnknownModelSlotError) as excinfo:
        parse_slot("generation")

    message = str(excinfo.value)
    assert "generation" in message
    for slot in ModelSlot:
        assert slot.value in message


def test_the_selection_cannot_be_mutated_after_resolution() -> None:
    """Frozen, because it is resolved once per turn precisely so that nothing can move it
    between two supersteps."""
    with pytest.raises((AttributeError, TypeError)):
        ModelSelection("a", "b", "c", "d", "e").chat = "something else"  # type: ignore[misc]


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def bound_sessionmaker(db_connection: AsyncConnection, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Point `tools.set_model` at the test's transaction.

    A real short-lived session per call, bound to the connection the fixture rolls back —
    the `app` fixture's `_stream_sessionmaker` pattern, and for its reason: the tool commits,
    and a commit on a joined session releases a savepoint rather than escaping the test.
    """
    import tools.set_model as cli

    def _sessionmaker():  # noqa: ANN202
        def _make() -> AsyncSession:
            return AsyncSession(
                bind=db_connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

        return _make

    monkeypatch.setattr(cli, "get_sessionmaker", _sessionmaker)
    return cli


class _RefusingClient:
    """A chat client whose provider does not serve the id."""

    def __init__(self) -> None:
        self.verified: list[str] = []
        self.closed = False

    async def verify_model(self, model_id: str) -> None:
        from app.services.llm import ChatConfigError

        self.verified.append(model_id)
        raise ChatConfigError(f"unknown chat model or endpoint: {model_id}")

    async def aclose(self) -> None:
        self.closed = True


async def test_the_cli_refuses_to_persist_a_model_the_provider_rejects(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-83(3): the probe exists to stop a typo becoming an outage, and it can only do that
    if the write does not happen. Generation fails **closed**, so a bad `chat` id makes every
    subsequent turn answer `LLM_ERROR`."""
    refusing = _RefusingClient()
    monkeypatch.setattr(bound_sessionmaker, "build_chat_client", lambda *_: refusing)

    exit_code = await bound_sessionmaker._set(ModelSlot.CHAT, "gtp-4o", verify=True)

    assert exit_code == 2
    assert refusing.verified == ["gtp-4o"]
    assert refusing.closed, "the probe client must be closed even on the refusal path"
    assert await ModelOverrideRepository(session).list_all() == []


async def test_the_cli_persists_a_model_the_provider_serves(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.llm import FakeChatClient

    fake = FakeChatClient()
    monkeypatch.setattr(bound_sessionmaker, "build_chat_client", lambda *_: fake)

    exit_code = await bound_sessionmaker._set(ModelSlot.ROUTER, "gpt-4o-mini", verify=True)

    assert exit_code == 0
    assert fake.verified == ["gpt-4o-mini"]
    assert (await resolve_models(session)).router == "gpt-4o-mini"


async def test_the_cli_can_skip_the_probe(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-verify` is for a host that cannot reach the provider. It must not reach for a
    client at all — building one would fail on exactly the network this flag exists for."""

    def _explode(*_: object) -> None:
        raise AssertionError("--no-verify must not build a chat client")

    monkeypatch.setattr(bound_sessionmaker, "build_chat_client", _explode)

    assert await bound_sessionmaker._set(ModelSlot.JUDGE, "o3-mini", verify=False) == 0
    assert (await resolve_models(session)).judge == "o3-mini"


async def test_the_cli_shows_which_slots_are_overridden(
    bound_sessionmaker, session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    """`show` has to distinguish an override from an environment default, or an operator
    cannot tell what a redeploy would change."""
    await set_model_override(session, slot=ModelSlot.RERANK, model_id="rerank-x", updated_by="test")
    await session.commit()
    # Drained *before* the call: `set_model_override` logs `config.model_override.set`, whose
    # own event name contains "override" and would be counted below.
    capsys.readouterr()

    assert await bound_sessionmaker._show() == 0

    rows = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(("chat", "router", "rerank", "judge"))
    ]
    assert len(rows) == len(ModelSlot)
    assert sum(line.endswith("override") for line in rows) == 1
    assert sum(line.endswith("env") for line in rows) == len(ModelSlot) - 1
    assert any("rerank-x" in line and line.endswith("override") for line in rows)


def test_the_cli_help_is_ascii() -> None:
    """R-80(7)'s lesson one tool over: a Windows `cp1252` console raises on anything else,
    and argparse writes `description` straight to stdout for `--help`."""
    from tools.set_model import _CLI_DESCRIPTION

    _CLI_DESCRIPTION.encode("ascii")
