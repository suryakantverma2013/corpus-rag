"""Runtime model selection (T-611, R-83) — which model id each call site calls, now.

**The shape of the thing.** :class:`ModelSelection` is the four ids a turn needs, resolved
once at turn start from `OPENAI_*` overlaid with whatever `model_overrides` holds. It is a
frozen dataclass of strings, so it is cheap to pass, impossible to mutate mid-turn, and
carries nothing that could not go in a log line.

**Resolved once per turn, carried on `RAGContext`.** Never on `RAGState`: that contract has
been unchanged for thirty-one tasks (R-42(2)), and this value must *not* survive a
checkpoint — a run resumed after an operator moved the answer model should use the model in
force now, which is the identical argument R-42(3) makes for authorization. Resolving once
rather than per node also means a turn cannot route with one model and generate with
another because an operator wrote between two supersteps.

**Nothing here selects a provider.** R-15 pins the vendor, and this picks among ids that
vendor serves; `LLM_BACKEND` / `EMBEDDING_BACKEND` are deliberately unreachable from this
module, because a database row that could turn `FakeChatClient` into a real one would put
~1,790 tests and CI onto the network.

**Embeddings is absent on purpose.** `OPENAI_EMBEDDING_MODEL` is an FR-ING-03 fingerprint
input read by the chunker, so a live change would leave existing chunks holding model-A
vectors while new ingests write model-B ones — and both are then compared in the same cosine
query, with nothing failing anywhere. That is not a degraded ranking, it is silently wrong
retrieval, and T-608 (the controlled re-embed FR-ING-03 implies and nothing provides) is the
prerequisite. See R-83(4); :data:`ModelSlot` has no member for it, so the omission is
unrepresentable rather than remembered.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories.model_overrides import ModelOverrideRepository

__all__ = [
    "ModelSelection",
    "ModelSlot",
    "UnknownModelSlotError",
    "clear_model_override",
    "parse_slot",
    "resolve_models",
    "set_model_override",
]

logger = structlog.get_logger(__name__)


class ModelSlot(enum.StrEnum):
    """The call sites an operator may repoint.

    Four rather than six. `embedding` is excluded by R-83(4) — see the module docstring.
    `judge_escalation` is here because T-314's escalation is *dormant while it equals*
    `judge` (`_scored` skips a second tier that would ask the same model twice), so an
    operator moving `judge` alone would silently arm escalation as a side effect.
    """

    CHAT = "chat"
    ROUTER = "router"
    RERANK = "rerank"
    JUDGE = "judge"
    JUDGE_ESCALATION = "judge_escalation"


class UnknownModelSlotError(ValueError):
    """A slot name outside :class:`ModelSlot`. Raised at the write path only."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """The model ids in force for one turn (or one evaluation job)."""

    chat: str
    router: str
    rerank: str
    judge: str
    judge_escalation: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ModelSelection:
        """The configured defaults, with no database read.

        This is what every call site gets when no override exists, and it is also the
        fallback when the override read fails — see :func:`resolve_models`.
        """
        openai = (settings or get_settings()).openai
        return cls(
            chat=openai.chat_model,
            router=openai.router_model,
            rerank=openai.rerank_model,
            judge=openai.judge_model,
            judge_escalation=openai.judge_escalation_model,
        )

    def for_slot(self, slot: ModelSlot) -> str:
        return getattr(self, slot.value)


async def resolve_models(session: AsyncSession, settings: Settings | None = None) -> ModelSelection:
    """The configured defaults overlaid with `model_overrides`.

    **Fails open to the configured defaults**, and that direction is forced: three of the
    four call sites already fail open (R-45(2) router, R-47(2) rerank, R-50(3) judge) while
    generation fails *closed* (R-48(2)) — so a database blip that raised here would convert
    a healthy turn into `LLM_ERROR` for the sake of a knob whose whole purpose is to avoid a
    restart. The env value is a correct answer, not a degraded one; it is what the
    deployment shipped with.

    A row naming a slot this version does not know is skipped rather than fatal. That is not
    defensive padding: a newer deploy may write a slot an older process is rolled back onto
    (embeddings, once T-608 lands), and the older process must keep answering.
    """
    settings = settings or get_settings()
    defaults = ModelSelection.from_settings(settings)
    try:
        # **The savepoint is what makes failing open safe, and it is not optional.** Postgres
        # aborts the whole transaction on a failed statement, so catching the error without
        # one would return the defaults and leave the *caller's* session poisoned — every
        # subsequent statement raising `InFailedSQLTransactionError` far from the cause. The
        # concrete case is not hypothetical: code deployed ahead of `alembic upgrade head`
        # finds no `model_overrides` table, and without this the turn that should have
        # degraded to the environment defaults fails outright.
        async with session.begin_nested():
            rows = await ModelOverrideRepository(session).list_all()
    except Exception:  # noqa: BLE001 — see the fail-open argument above.
        logger.warning("config.model_override.read_failed", exc_info=True)
        return defaults

    overrides: dict[str, str] = {}
    for row in rows:
        if row.slot not in ModelSlot:
            logger.warning("config.model_override.unknown_slot", slot=row.slot)
            continue
        overrides[row.slot] = row.model_id
    if not overrides:
        return defaults
    return ModelSelection(
        chat=overrides.get(ModelSlot.CHAT, defaults.chat),
        router=overrides.get(ModelSlot.ROUTER, defaults.router),
        rerank=overrides.get(ModelSlot.RERANK, defaults.rerank),
        judge=overrides.get(ModelSlot.JUDGE, defaults.judge),
        judge_escalation=overrides.get(ModelSlot.JUDGE_ESCALATION, defaults.judge_escalation),
    )


def parse_slot(name: str) -> ModelSlot:
    """``name`` as a :class:`ModelSlot`, or :class:`UnknownModelSlotError`.

    The message names every legal value, because the caller is a human at a shell who has
    just mistyped one and the alternative is an integrity error naming a constraint.
    """
    try:
        return ModelSlot(name)
    except ValueError as exc:
        legal = ", ".join(slot.value for slot in ModelSlot)
        raise UnknownModelSlotError(f"unknown slot {name!r} — expected one of: {legal}") from exc


async def set_model_override(
    session: AsyncSession, *, slot: ModelSlot, model_id: str, updated_by: str | None
) -> None:
    """Point ``slot`` at ``model_id``. The caller commits, and validates first.

    Validation is **not** done here, and that is deliberate: it is a network call, this
    function is inside the caller's transaction, and holding one open across a provider
    round trip is the pool-pinning failure R-41(7) already ruled on. See
    `tools.set_model`, which probes and then writes.
    """
    await ModelOverrideRepository(session).set(
        slot=slot.value, model_id=model_id, updated_by=updated_by
    )
    logger.info(
        "config.model_override.set",
        slot=slot.value,
        model_id=model_id,
        updated_by=updated_by,
    )


async def clear_model_override(session: AsyncSession, *, slot: ModelSlot) -> bool:
    """Revert ``slot`` to its configured default. True if an override existed."""
    existed = await ModelOverrideRepository(session).clear(slot=slot.value)
    if existed:
        logger.info("config.model_override.cleared", slot=slot.value)
    return existed
