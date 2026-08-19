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

**The probe before the write is the point of this tool.** Three of the six slots fail open
— a bad router, reranker or judge id costs a wasted call and a degraded stage (R-45(2),
R-47(2), R-50(3)) — but generation fails **closed** (R-48(2)), so a typo there makes every
subsequent turn answer `LLM_ERROR`. `set` therefore asks the provider whether it serves the
id *before* persisting it, which turns an outage into a rejected command. `--no-verify`
exists for the deployment whose network cannot reach the provider from wherever this runs;
it is the operator taking that risk knowingly, and it says so.

**Embeddings is the sixth slot and it is not like the other five** (T-612, R-87). The other
five change what the *next call* asks. This one changes what gets **written**: the id is an
FR-ING-03 fingerprint input, so from the moment it moves, new chunks land in a different
vector space from the existing ones and both are compared in the same cosine query. R-83(4)
refused the slot on exactly that ground, and none of it stopped being true — T-608 changed
the *consequence*, by making the drift visible (the staleness report reads the provenance
each chunk recorded) and finite (`tools.reembed run` converts it). So this write path differs
from the other five in two ways, both deliberate:

* it **prices the flip before writing** and refuses without `--yes` when the corpus would be
  left holding two spaces — an operator should not learn that cost from the next invoice; and
* it **refuses `--no-verify`**, because the probe here is not asking whether the id exists.
  The column is `VECTOR(3072)` and nothing widens it at runtime, so the probe embeds one
  string and measures what comes back. No offline source answers that question, and the
  failure it prevents is a corpus that will not ingest at all rather than a degraded stage.

Stdout is ASCII. R-80(7)'s lesson, one tool over: a Windows `cp1252` console raises
`UnicodeEncodeError` on anything else, and `--help` reaches stdout too.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.config import get_settings
from app.db.base import EMBEDDING_DIM
from app.db.session import get_sessionmaker
from app.services.embeddings import (
    EmbeddingDimensionError,
    EmbeddingError,
    build_embedding_client,
)
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
from app.services.reembed import configured_pipeline, plan_reembed

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
    "The embedding slot is different: OPENAI_EMBEDDING_MODEL is an FR-ING-03\n"
    "fingerprint input, so moving it leaves existing chunks in the old vector space\n"
    "until they are rebuilt. `set embedding` prices the flip first and needs --yes to\n"
    "proceed, and refuses --no-verify because its probe measures the vector dimension,\n"
    "which cannot be checked offline. Drain the backlog with:\n"
    "  python -m tools.reembed run --limit N"
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


async def _set_embedding(model_id: str, *, verify: bool, yes: bool) -> int:
    """The sixth slot's write path, which differs from the other five on purpose (R-87(3)).

    Two refusals and a price. The refusals are not extra caution — each prevents a failure the
    other five slots cannot have.
    """
    settings = get_settings()

    if not verify:
        # There is nothing for `--no-verify` to skip here that would still leave a useful
        # check behind. The probe is not "does this id exist" (the runtime would find that
        # out in one wasted call) but "does it return 3072 numbers", and no offline source
        # answers that. Setting it unchecked risks a corpus that refuses to ingest at all.
        print("refused: --no-verify is not available for the embedding slot.", file=sys.stderr)
        print(
            "the probe measures the vector dimension against the fixed "
            f"VECTOR({EMBEDDING_DIM}) column, and nothing offline can tell you that.",
            file=sys.stderr,
        )
        return 2

    # Probed before the transaction opens, for `_set`'s reason. One real embed call: it
    # establishes existence and dimension together, which no pair of cheaper checks does.
    client = build_embedding_client(settings)
    try:
        await client.embed_query("dimension probe", model=model_id)
    except EmbeddingDimensionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print(
            "the column is fixed; a different dimension needs an ALTER and a full "
            "re-embed, which is a migration rather than a setting.",
            file=sys.stderr,
        )
        return 2
    except EmbeddingError as exc:
        print(f"refused: the provider did not embed with {model_id!r} ({exc})", file=sys.stderr)
        return 2
    finally:
        await client.aclose()

    # Price the flip *before* writing, against the candidate rather than the id in force.
    async with get_sessionmaker()() as session:
        plan = await plan_reembed(
            session,
            limit=1,
            settings=settings,
            pipeline=configured_pipeline(settings, embedding_model=model_id),
        )
    totals = plan.totals
    if totals.documents:
        print(
            f"this flip strands {totals.documents} document(s) / {totals.chunks} chunk(s) "
            f"(~{totals.token_count:,} tokens) in the previous vector space."
        )
        print("they keep answering, from the old space, until rebuilt with:")
        print("  python -m tools.reembed run --limit N")
        if not yes:
            print("refused: re-run with --yes to accept that cost.", file=sys.stderr)
            return 2

    async with get_sessionmaker()() as session:
        await set_model_override(
            session, slot=ModelSlot.EMBEDDING, model_id=model_id, updated_by=_operator()
        )
        await session.commit()
    print(f"{ModelSlot.EMBEDDING.value} -> {model_id}")
    return 0


async def _set(slot: ModelSlot, model_id: str, *, verify: bool, yes: bool = False) -> int:
    if slot is ModelSlot.EMBEDDING:
        return await _set_embedding(model_id, verify=verify, yes=yes)

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
    set_parser.add_argument(
        "--yes",
        action="store_true",
        help="accept the re-embed cost when moving the embedding slot",
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
        return asyncio.run(_set(slot, args.model_id, verify=not args.no_verify, yes=args.yes))
    return asyncio.run(_clear(slot))


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
