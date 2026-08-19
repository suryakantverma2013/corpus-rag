"""The FR-ING-03 re-embed trigger, from the command line (T-608, R-84).

Run it::

    cd backend && uv run python -m tools.reembed                  # what is stale, and what it costs
    cd backend && uv run python -m tools.reembed run --limit 50   # re-drive 50 of them
    cd backend && uv run python -m tools.reembed --owner <uuid>   # one user's documents

**What this exists for.** `embedding_fingerprint` folds in `embedding_model`,
`chunking_version` and `preprocessing_version`, so changing `OPENAI_EMBEDDING_MODEL`, retuning a
`CHUNKER_*` knob or bumping `PREPROCESSING_VERSION` correctly invalidates every stored
fingerprint — and until T-608 nothing could act on that. FR-ING-04's `ACTIVE` short-circuit made
a same-version job a no-op, `/retry` is `FAILED`-only and `/replace` with identical bytes queues
nothing, so the corpus kept serving old-pipeline vectors indefinitely: correctly, and forever.

**`plan` is the default because a fleet-wide re-embed is a cost event, not a button** (R-84(5)).
It is read-only and safe against production at any moment, and it prints the estimated embedding
tokens beside the document count, because those are the same sentence to the database and very
different sentences to whoever is paying.

**`run` requires `--limit`.** There is deliberately no way to type "all of them": R-84(9) makes
the work resumable by construction — a rebuilt document is no longer stale — so the operator
loop is `plan`, `run --limit N`, let the worker drain, `plan` again. A document already being
rebuilt is excluded by the enumeration itself, so re-running mid-drain cannot double-queue.

Only `ACTIVE` documents are eligible (R-84(4)); a stale `FAILED` one is already reachable
through `POST /documents/{id}/retry`, which rebuilds it under the configured pipeline anyway.

This needs the **worker running** to make any progress: `run` queues jobs, it does not perform
them. Redis, Postgres and the object store must all be reachable.

Stdout is ASCII. R-80(7)'s lesson, one tool over: a Windows `cp1252` console raises
`UnicodeEncodeError` on anything else, and `--help` reaches stdout too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.services.documents import (
    DocumentNotFoundError,
    NotRebuildableError,
    NotStaleError,
    OriginalCorruptError,
    rebuild_document,
)
from app.services.jobs import build_job_queue
from app.services.object_storage import ObjectStorageError, build_object_storage
from app.services.reembed import DEFAULT_PAGE, MAX_PAGE, plan_reembed

_CLI_DESCRIPTION = (
    "Find and re-drive documents whose embeddings predate the configured pipeline.\n"
    "\n"
    "`plan` (the default) is read-only. `run --limit N` queues N rebuilds and needs the\n"
    "arq worker running to make progress; it returns as soon as they are queued.\n"
    "\n"
    "Staleness is read from each chunk's stored provenance, so this covers all three\n"
    "non-text inputs to the FR-ING-03 fingerprint: the embedding model, the composite\n"
    "chunking version (which folds in the CHUNKER_* knobs) and the preprocessing version.\n"
    "\n"
    "A rebuild copies the stored original forward to the next version and re-embeds the\n"
    "whole document. The version already indexed keeps answering questions until the new\n"
    "one is built, so this is safe to run against a live deployment - but it is not free."
)


def _pipeline_lines(plan) -> list[str]:  # noqa: ANN001 — a ReembedPlan; annotating it adds nothing
    return [
        "configured pipeline (what the next ingestion will fingerprint with):",
        f"  embedding_model       {plan.pipeline.embedding_model}",
        f"  chunking_version      {plan.pipeline.chunking_version}",
        f"  preprocessing_version {plan.pipeline.preprocessing_version}",
    ]


def _totals_lines(plan) -> list[str]:  # noqa: ANN001
    totals = plan.totals
    lines = [
        f"stale: {totals.documents} document(s), {totals.chunks} chunk(s), "
        f"~{totals.token_count:,} embedding tokens to re-spend",
    ]
    if totals.in_flight:
        # Reported, never folded into the count: a document already being rebuilt is neither
        # work to do nor work done, and omitting it reads as "nothing left" mid-run.
        lines.append(f"       {totals.in_flight} more already queued or running - leave those be")
    return lines


def _table(plan) -> list[str]:  # noqa: ANN001
    if not plan.documents:
        return []
    width = min(44, max(len(row.filename) for row in plan.documents))
    lines = [
        "",
        f"{'document_id':36}  {'file':{width}}  {'v':>3}  {'chunks':>6}  {'tokens':>9}  drifted",
        "-" * (36 + width + 40),
    ]
    for row in plan.documents:
        name = row.filename if len(row.filename) <= width else row.filename[: width - 3] + "..."
        lines.append(
            f"{row.document_id!s:36}  {name:{width}}  {row.document_version:>3}  "
            f"{row.chunk_count:>6}  {row.token_count:>9,}  {','.join(row.drifted_inputs)}"
        )
    if len(plan.documents) < plan.totals.documents:
        # No silent caps: a page that looks like the whole set is how "covered everything" gets
        # believed about a bounded read.
        lines.append(
            f"... showing {len(plan.documents)} of {plan.totals.documents}; "
            "raise --limit or pass --offset to see more"
        )
    return lines


async def _plan(owner_id: uuid.UUID | None, limit: int, offset: int) -> int:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        plan = await plan_reembed(
            session, owner_id=owner_id, limit=limit, offset=offset, settings=settings
        )

    lines = [*_pipeline_lines(plan), "", *_totals_lines(plan)]
    if not plan.totals.documents:
        lines.append("")
        lines.append("nothing to do: every ACTIVE document was built by this pipeline.")
    lines.extend(_table(plan))
    print("\n".join(lines))
    return 0


async def _run(owner_id: uuid.UUID | None, limit: int) -> int:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    storage = build_object_storage(settings)
    queue = build_job_queue(settings)

    try:
        async with sessionmaker() as session:
            plan = await plan_reembed(session, owner_id=owner_id, limit=limit, settings=settings)

        print("\n".join([*_pipeline_lines(plan), "", *_totals_lines(plan)]))
        if not plan.documents:
            print("\nnothing queued.")
            return 0
        print(f"\nqueueing {len(plan.documents)} rebuild(s):")

        queued = refused = 0
        for row in plan.documents:
            # A fresh session per document: each rebuild is its own unit of work, and one
            # refusal must not leave a rolled-back session for the next.
            async with sessionmaker() as session:
                try:
                    outcome = await rebuild_document(
                        document_id=row.document_id,
                        # No principal here, and the column is nullable for exactly this
                        # (R-84(5)). Attributing an operator's action to the document's owner
                        # would be worse than recording no actor.
                        actor_id=None,
                        session=session,
                        storage=storage,
                        queue=queue,
                        settings=settings,
                    )
                except (
                    DocumentNotFoundError,
                    NotRebuildableError,
                    NotStaleError,
                    OriginalCorruptError,
                ) as exc:
                    # Report and carry on. The enumeration was taken before the first rebuild,
                    # so a document that has moved since is expected rather than exceptional --
                    # `rebuild_document` re-checks under its row lock, which is what makes
                    # continuing safe instead of merely convenient.
                    refused += 1
                    print(f"  skipped  {row.document_id}  {type(exc).__name__}: {exc}")
                    continue
                except ObjectStorageError as exc:
                    # Not one document's problem. Every remaining rebuild reads the object store
                    # too, so continuing would print the same failure N times and queue nothing.
                    print(f"  FAILED   {row.document_id}  object storage: {exc}", file=sys.stderr)
                    print(
                        f"aborted after {queued} queued: the object store is unreachable.",
                        file=sys.stderr,
                    )
                    return 1

            queued += 1
            print(
                f"  queued   {outcome.document_id}  v{outcome.previous_version}->v{outcome.version}"
                f"  {row.chunk_count} chunk(s)  ~{row.token_count:,} tokens  job {outcome.job_id}"
            )

        remaining = plan.totals.documents - queued
        print(f"\nqueued {queued}, skipped {refused}.")
        if remaining > 0:
            print(f"{remaining} still stale - re-run once the worker has drained.")
        print("the worker does the work; nothing is re-embedded until it runs.")
        return 0
    finally:
        await queue.aclose()
        await storage.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.reembed",
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--owner", type=uuid.UUID, default=None, help="narrow to one owner's documents"
    )
    sub = parser.add_subparsers(dest="command")

    plan_cmd = sub.add_parser("plan", help="list stale documents and their cost (read-only)")
    plan_cmd.add_argument("--limit", type=int, default=DEFAULT_PAGE, help="rows to show")
    plan_cmd.add_argument("--offset", type=int, default=0, help="rows to skip")

    run_cmd = sub.add_parser("run", help="queue rebuilds for stale documents")
    run_cmd.add_argument(
        "--limit",
        type=int,
        required=True,
        help=f"how many documents to re-drive (max {MAX_PAGE}); required, deliberately",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args.owner, args.limit))
    limit = getattr(args, "limit", DEFAULT_PAGE)
    offset = getattr(args, "offset", 0)
    return asyncio.run(_plan(args.owner, limit, offset))


if __name__ == "__main__":  # pragma: no cover — entrypoint
    raise SystemExit(main())
