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
        embedding=openai.embedding_model,
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

    Not defensive padding: `embedding` was exactly this case until T-612 added it, and the
    next slot will be too — a process rolled back to an older build has to keep answering
    while a newer one's row sits in the table. Re-pointed at a name no version has ever
    known, because the moment `embedding` became real this test stopped testing anything.
    """
    await session.execute(
        text("INSERT INTO model_overrides (slot, model_id) VALUES ('summariser', 'whatever')")
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


def test_the_embedding_slot_exists_and_what_it_costs_to_use(
    session: AsyncSession,
) -> None:
    """R-87, the successor to R-83(4)'s pin — **rewritten rather than deleted**.

    The hazard R-83(4) named is unchanged and always will be: `OPENAI_EMBEDDING_MODEL` is an
    FR-ING-03 fingerprint input read by the chunker, so a flip leaves existing chunks holding
    model-A vectors while new ingests write model-B ones, and both are then compared in the
    same cosine query with nothing failing anywhere.

    What changed is the *recovery*, and that is the whole basis on which this slot is
    admitted. T-608 made the drift **visible** — the staleness report reads the provenance
    each chunk recorded, so an operator can enumerate exactly which documents are in the old
    space — and **finite**, because `tools.reembed run` converts them. The window is now a
    state you can price and drain rather than one you cannot see.

    Three properties hold it together, and each is asserted somewhere that fails loudly:

    * the id is resolved **per ingest job** and travels on `ChunkedDocument`, so the
      fingerprint and the vector cannot name different models (`test_ingest_task.py`);
    * staleness is measured against the **resolved** id, not the environment default, or the
      report would answer a question nobody asked (`test_reembed.py`);
    * the write path refuses a model whose dimension is not `EMBEDDING_DIM`, because the
      column is fixed and a mismatch is a corpus that will not ingest (`test_set_model`
      below).

    If you are here because you want to remove one of those, this docstring is the argument
    for why the slot exists at all.
    """
    assert "embedding" in {slot.value for slot in ModelSlot}
    assert ModelSelection.from_settings().embedding == get_settings().openai.embedding_model
    assert ModelSelection("a", "b", "c", "d", "e", "f").for_slot(ModelSlot.EMBEDDING) == "f", (
        "for_slot reads by the enum's value, so the field name and the slot must not drift"
    )


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
        ModelSelection("a", "b", "c", "d", "e", "f").chat = "x"  # type: ignore[misc]


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

    # Prefixes derived from the enum, never listed by hand: the hand-written tuple missed
    # `embedding` the moment T-612 added it, and the count assertion below then failed for a
    # reason that had nothing to do with what this test is about.
    rows = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(tuple(slot.value for slot in ModelSlot))
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


# --- the embedding slot's write path (T-612, R-87(3)) -------------------------


async def _seed_one_active_chunk(session: AsyncSession) -> None:
    """One `ACTIVE` document with one chunk whose provenance is the pipeline in force.

    The minimum a flip can strand. Written here rather than imported from `test_reembed.py`
    so this file states what "a corpus that would be stranded" means for the assertion below.
    """
    import uuid

    from app.db.base import DEFAULT_TENANT_ID
    from app.db.enums import DocumentStatus
    from app.db.models.document import Document
    from app.db.models.document_chunk import DocumentChunk
    from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
    from app.db.repositories.users import UserRepository
    from app.ingestion.chunker import effective_chunking_version
    from app.ingestion.parsers.base import PREPROCESSING_VERSION

    settings = get_settings()
    owner = uuid.uuid4()
    await UserRepository(session).upsert_from_claims(
        sub=owner, email=f"{owner.hex[:8]}@corpus.local", display_name="Owner"
    )
    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner)
    document = Document(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        owner_id=owner,
        filename="handbook.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        checksum_sha256=uuid.uuid4().hex * 2,
        storage_uri="file://seed",
        status=DocumentStatus.ACTIVE,
        current_version=1,
        searchable=True,
    )
    session.add(document)
    await session.flush()
    session.add(
        DocumentChunk(
            document_id=document.id,
            document_version=1,
            chunk_index=0,
            chunk_hash=uuid.uuid4().hex,
            embedding_fingerprint=uuid.uuid4().hex,
            token_count=20,
            tenant_id=DEFAULT_TENANT_ID,
            knowledge_base_id=kb.id,
            chunk_text="a passage",
            meta={
                "embedding_model": settings.openai.embedding_model,
                "chunking_version": effective_chunking_version(settings.chunker),
                "preprocessing_version": PREPROCESSING_VERSION,
            },
        )
    )
    await session.flush()


class _WrongDimensionClient:
    """An embedding client whose model returns vectors the column cannot hold."""

    def __init__(self) -> None:
        self.asked: list[str | None] = []
        self.closed = False

    async def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        from app.db.base import EMBEDDING_DIM
        from app.services.embeddings import _check_dimensions

        self.asked.append(model)
        _check_dimensions([0.0] * (EMBEDDING_DIM // 2), model=model or "unknown")
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.closed = True


async def test_the_embedding_probe_refuses_a_model_of_the_wrong_dimension(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal the other five slots have no equivalent of.

    `document_chunks.embedding` is `VECTOR(EMBEDDING_DIM)` and nothing widens it at runtime,
    so a model of another size is not a degraded choice — it is a corpus that cannot ingest at
    all. Caught at the operator's keystroke rather than at the next upload, and caught by
    *measuring* rather than by consulting a list of model ids, which would rot the day the
    provider ships anything.
    """
    wrong = _WrongDimensionClient()
    monkeypatch.setattr(bound_sessionmaker, "build_embedding_client", lambda *_: wrong)

    exit_code = await bound_sessionmaker._set(
        ModelSlot.EMBEDDING, "text-embedding-3-small", verify=True, yes=True
    )

    assert exit_code == 2
    assert wrong.asked == ["text-embedding-3-small"], "the probe must ask for the candidate"
    assert wrong.closed, "the probe client must be closed on the refusal path too"
    assert await ModelOverrideRepository(session).list_all() == [], "nothing may be written"


async def test_the_embedding_slot_refuses_no_verify(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-verify` is coherent for the other five and incoherent here.

    There it means "I accept the risk that this id is wrong", and the risk is a degraded
    stage. Here the probe is not asking whether the id exists but how many numbers it
    returns, and no offline source answers that — so the flag would be accepting a risk it
    cannot describe. It must also not build a client, since that is the very network the flag
    exists for.
    """

    def _explode(*_: object) -> None:
        raise AssertionError("--no-verify must not build an embedding client")

    monkeypatch.setattr(bound_sessionmaker, "build_embedding_client", _explode)

    exit_code = await bound_sessionmaker._set(
        ModelSlot.EMBEDDING, "whatever", verify=False, yes=True
    )

    assert exit_code == 2
    assert await ModelOverrideRepository(session).list_all() == []


async def test_moving_the_embedding_slot_prices_the_flip_and_needs_yes(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # noqa: ANN001
    """R-87(3): the operator is told what the flip costs *before* it happens.

    The other five slots are free to move and free to move back. This one leaves every
    existing chunk in the previous vector space until a rebuild drains it, so the refusal is
    not paternalism — it is the difference between an operator who chose that backlog and one
    who discovers it on an invoice. `--yes` is the acceptance, and it is a flag rather than an
    interactive prompt because every tool here is scriptable.
    """
    from app.services.embeddings import FakeEmbeddingClient

    monkeypatch.setattr(
        bound_sessionmaker, "build_embedding_client", lambda *_: FakeEmbeddingClient()
    )
    await _seed_one_active_chunk(session)

    refused = await bound_sessionmaker._set(
        ModelSlot.EMBEDDING, "operators-choice", verify=True, yes=False
    )

    assert refused == 2
    priced = capsys.readouterr().out
    assert "strands" in priced and "tools.reembed run" in priced, (
        "the refusal has to say what the cost is and how to pay it down"
    )
    assert await ModelOverrideRepository(session).list_all() == []

    accepted = await bound_sessionmaker._set(
        ModelSlot.EMBEDDING, "operators-choice", verify=True, yes=True
    )

    assert accepted == 0
    assert (await resolve_models(session)).embedding == "operators-choice"


async def test_an_empty_corpus_needs_no_yes(
    bound_sessionmaker, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to strand, nothing to accept.

    A first-day deployment choosing its embedding model should not have to acknowledge a
    backlog that does not exist — and pricing a flip at zero is exactly how an operator can
    tell that it is safe.

    The corpus is emptied inside this test's own transaction rather than assumed empty: the
    tool prices the **whole deployment**, deliberately (an operator wants the global number),
    so on a development database this assertion would otherwise be a statement about whatever
    the last end-to-end run happened to leave behind.
    """
    from sqlalchemy import delete

    from app.db.models.document import Document
    from app.db.models.document_chunk import DocumentChunk
    from app.db.models.document_figure import DocumentFigure
    from app.db.models.knowledge_job import KnowledgeJob
    from app.services.embeddings import FakeEmbeddingClient

    # Children first: all three reference `documents` with NO ACTION, which is R-39's deliberate
    # choice so a delete has to be explicit about what it is destroying.
    #
    # `document_figures` (T-714) is the newest of them, and it was missing here until T-716 put
    # the first figure rows on a development database — this cleanup passes on an empty corpus
    # whatever it forgets, so the omission is invisible until somebody has the row it misses.
    await session.execute(delete(DocumentChunk))
    await session.execute(delete(DocumentFigure))
    await session.execute(delete(KnowledgeJob))
    await session.execute(delete(Document))
    await session.flush()

    monkeypatch.setattr(
        bound_sessionmaker, "build_embedding_client", lambda *_: FakeEmbeddingClient()
    )

    exit_code = await bound_sessionmaker._set(
        ModelSlot.EMBEDDING, "operators-choice", verify=True, yes=False
    )

    assert exit_code == 0
    assert (await resolve_models(session)).embedding == "operators-choice"
