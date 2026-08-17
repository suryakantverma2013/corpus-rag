"""Operator-selectable models at runtime (T-611, R-83) — show, set and clear.

Run it::

    cd backend && uv run python -m tools.set_model                       # what is in force
    cd backend && uv run python -m tools.set_model set chat gpt-4o-mini
    cd backend && uv run python -m tools.set_model clear chat

**Why a tool rather than a route.** The subject is deployment configuration, the actor is an
operator holding database credentials, and there is no admin surface in the product to hang
it on — §4 has no settings screen, and inventing one is an NFR-VIS-01 conversation this
change does not need. `tools.spec_xref` (T-607) and `tools.feedback_calibration` (T-609) set
the precedent. A `PUT /api/v1/admin/models` is purely additive later, and the day it ships it
brings an *authenticated administrator*, which is also the day an NFR-SEC-08 audit row
becomes both possible and right (see `app.db.models.model_override`).

**The probe before the write is the point of this tool.** Three of the four slots fail open
— a bad router, reranker or judge id costs a wasted call and a degraded stage (R-45(2),
R-47(2), R-50(3)) — but generation fails **closed** (R-48(2)), so a typo there makes every
subsequent turn answer `LLM_ERROR`. `set` therefore asks the provider whether it serves the
id *before* persisting it, which turns an outage into a rejected command. `--no-verify`
exists for the deployment whose network cannot reach the provider from wherever this runs;
it is the operator taking that risk knowingly, and it says so.

**Embeddings is absent and that is not an oversight.** `OPENAI_EMBEDDING_MODEL` is an
FR-ING-03 fingerprint input, so changing it live would leave old chunks holding model-A
vectors and new ones model-B, compared in the same cosine query with nothing failing
anywhere. T-608 owns the controlled re-embed that has to come first (R-83(4)).

Stdout is ASCII. R-80(7)'s lesson, one tool over: a Windows `cp1252` console raises
`UnicodeEncodeError` on anything else, and `--help` reaches stdout too.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.services.llm import ChatError, build_chat_client
from app.services.model_selection import (
    ModelSelection,
    ModelSlot,
    UnknownModelSlotError,
    clear_model_override,
    parse_slot,
    resolve_models,
    set_model_override,
)

#: `--help` text. Not `__doc__` — see the module docstring's last paragraph.
_CLI_DESCRIPTION = (
    "Show, set or clear the model each call site uses, without a restart.\n"
    "\n"
    "Slots: " + ", ".join(slot.value for slot in ModelSlot) + "\n"
    "\n"
    "An unset slot uses its OPENAI_* environment default. `clear` returns a slot to\n"
    "that default; there is no way to 'set' a slot back to the default by typing the\n"
    "same id, because that would leave a row pinning a value the deployment could no\n"
    "longer move by redeploying.\n"
    "\n"
    "Embeddings is deliberately not a slot: OPENAI_EMBEDDING_MODEL is an FR-ING-03\n"
    "fingerprint input, so changing it live splits the corpus into two vector spaces\n"
    "that are then compared in the same query. T-608 owns the re-embed that must come\n"
    "first."
)


def _describe(selection: ModelSelection, overridden: set[str]) -> str:
    """One line per slot: the id in force, and where it came from."""
    width = max(len(slot.value) for slot in ModelSlot)
    lines = ["slot".ljust(width) + "  model" + " " * 26 + "source", "-" * (width + 40)]
    for slot in ModelSlot:
        source = "override" if slot.value in overridden else "env"
        lines.append(f"{slot.value.ljust(width)}  {selection.for_slot(slot).ljust(30)} {source}")
    return "\n".join(lines)


async def _overridden_slots() -> set[str]:
    from app.db.repositories.model_overrides import ModelOverrideRepository

    async with get_sessionmaker()() as session:
        return {row.slot for row in await ModelOverrideRepository(session).list_all()}


async def _show() -> int:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        selection = await resolve_models(session, settings)
    print(_describe(selection, await _overridden_slots()))
    return 0


async def _set(slot: ModelSlot, model_id: str, *, verify: bool) -> int:
    settings = get_settings()
    if verify:
        # Verified *before* the transaction opens, never inside it: a provider round trip
        # inside a write transaction pins a pool slot and the cluster's xmin horizon for its
        # duration, which is the T-205 rule `workers/evaluate.py` states at length.
        client = build_chat_client(settings)
        try:
            await client.verify_model(model_id)
        except ChatError as exc:
            print(f"refused: the provider does not serve {model_id!r} ({exc})", file=sys.stderr)
            print("re-run with --no-verify to set it anyway.", file=sys.stderr)
            return 2
        finally:
            await client.aclose()
    else:
        print("warning: --no-verify, the id was not checked against the provider.")
        if slot is ModelSlot.CHAT:
            # Named for this slot only, because this is the one where being wrong is an
            # outage rather than a degradation.
            print("warning: generation fails closed - a wrong id here fails every turn.")

    async with get_sessionmaker()() as session:
        await set_model_override(session, slot=slot, model_id=model_id, updated_by=_operator())
        await session.commit()
    print(f"{slot.value} -> {model_id}")
    return 0


async def _clear(slot: ModelSlot) -> int:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        existed = await clear_model_override(session, slot=slot)
        await session.commit()
        selection = await resolve_models(session, settings)
    if not existed:
        print(f"{slot.value} had no override; still {selection.for_slot(slot)} (env)")
        return 0
    print(f"{slot.value} -> {selection.for_slot(slot)} (env default restored)")
    return 0


def _operator() -> str | None:
    """Whoever ran this, best effort. Diagnostic only — never an authorization decision."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no controlling terminal, a container without passwd
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.set_model",
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("show", help="print the model in force for every slot (the default)")

    set_parser = sub.add_parser("set", help="point a slot at a model id")
    set_parser.add_argument("slot", help="one of: " + ", ".join(s.value for s in ModelSlot))
    set_parser.add_argument("model_id", help="the provider model id, e.g. gpt-4o-mini")
    set_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the provider check (you accept the risk of a wrong id)",
    )

    clear_parser = sub.add_parser("clear", help="revert a slot to its environment default")
    clear_parser.add_argument("slot", help="one of: " + ", ".join(s.value for s in ModelSlot))

    args = parser.parse_args(argv)

    if args.command in (None, "show"):
        return asyncio.run(_show())

    try:
        slot = parse_slot(args.slot)
    except UnknownModelSlotError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "set":
        return asyncio.run(_set(slot, args.model_id, verify=not args.no_verify))
    return asyncio.run(_clear(slot))


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
