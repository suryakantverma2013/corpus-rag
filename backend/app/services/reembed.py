"""The FR-ING-03 re-embed trigger's read side (T-608, R-84).

`embedding_fingerprint = SHA-256(chunk_text | embedding_model | chunking_version |
preprocessing_version)` correctly invalidates every stored fingerprint when any of its three
non-text inputs moves — and until T-608 **nothing could act on that**. FR-ING-04's `ACTIVE`
short-circuit makes a same-version job a no-op, `/retry` is `FAILED`-only (R-39(5)) and
`/replace` with identical bytes queues nothing (R-40(1)), so after changing
`OPENAI_EMBEDDING_MODEL`, retuning a `CHUNKER_*` knob or bumping `PREPROCESSING_VERSION` an
operator could re-embed exactly nothing and the corpus kept serving old-pipeline vectors
indefinitely — correctly, and forever.

This module answers the two questions that have to be answered *before* spending money:
**what is the configured pipeline** and **which documents were not built by it**. The write
side is `app.services.documents.rebuild_document`, and both the admin route and
`tools.reembed` go through the same two functions — one implementation, two entry points, the
§8.53(5) `ByteSource` lesson applied to a second reader instead of a second writer.

Nothing here mutates anything. `plan_reembed` is safe to run against production at any time,
which is the property that makes "look before you pay" a habit rather than a instruction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories.chunks import (
    DocumentChunkRepository,
    StalePipelineDocument,
    StalePipelineTotals,
)
from app.ingestion.chunker import effective_chunking_version

# `parsers.base`, not the `parsers` package: the package imports the concrete parsers and with
# them PyMuPDF and python-docx, and this module is reachable from the API process, which has
# never needed either. `base` is the contract module and its only import is `app.config`.
from app.ingestion.parsers.base import PREPROCESSING_VERSION
from app.services.model_selection import resolve_models

#: Default page for a bounded read. Deliberately small: the first thing an operator does with
#: this is read it, and R-84(9)'s resumability means a page is a *unit of work*, not a
#: limitation — re-running after the worker drains shows what is left.
DEFAULT_PAGE = 50

#: Hard ceiling on one page, matching `GET /api/v1/users`. A fleet-wide re-embed is a cost
#: event (R-84(5)), so the surface that enumerates it is bounded too.
MAX_PAGE = 500


@dataclass(frozen=True, slots=True)
class ConfiguredPipeline:
    """The three non-text inputs to FR-ING-03's fingerprint, as configured *right now*."""

    embedding_model: str
    chunking_version: str
    preprocessing_version: str


def configured_pipeline(
    settings: Settings | None = None, *, embedding_model: str | None = None
) -> ConfiguredPipeline:
    """What the next ingestion will fingerprint with.

    `embedding_model` defaults to `OPENAI_EMBEDDING_MODEL` and is **overridden by the
    operator's runtime slot when one is set** — pass the resolved id, or use
    :func:`resolved_pipeline`, which does the read for you. Since T-612 the environment is
    only the fallback, and a report built on the fallback while a job embeds with the
    override would compare every chunk against a model nothing uses: with the override
    pointing anywhere else, the whole corpus reads as stale, and after a rebuild the whole
    corpus reads as stale still. The comparison is one SQL predicate over recorded
    provenance, so nothing would fail — it would simply answer the wrong question forever.

    Still read from configuration rather than from an `EmbeddingClient`, and that argument is
    unchanged by the override: constructing a client to ask it its own name would make a
    report depend on the provider being reachable, and a re-embed report you cannot produce
    during an outage is one you cannot use to explain the outage. The override is a database
    row, and this function's callers already hold a session.

    `test_reembed.py` pins the setting/client equivalence for both backends rather than
    trusting this paragraph: it is exactly the drift that would make the whole tool quietly
    lie.

    `chunking_version` is the **composite** (R-35(8)) — `"1/2000/200/200/0.5"` — so a tuned
    `CHUNKER_TARGET_CHARS` registers here as drift, which is the FR-ING-03 trigger the spec
    describes and nothing could previously act on either.
    """
    settings = settings or get_settings()
    return ConfiguredPipeline(
        embedding_model=embedding_model or settings.openai.embedding_model,
        chunking_version=effective_chunking_version(settings.chunker),
        preprocessing_version=PREPROCESSING_VERSION,
    )


async def resolved_pipeline(
    session: AsyncSession, settings: Settings | None = None
) -> ConfiguredPipeline:
    """:func:`configured_pipeline` with `ModelSlot.EMBEDDING` resolved against the database.

    The one entry point every staleness question should use, so that "what the next ingestion
    will fingerprint with" means the same thing here as it does in `workers/ingest.py`, which
    resolves the same slot per job.
    """
    settings = settings or get_settings()
    models = await resolve_models(session, settings)
    return configured_pipeline(settings, embedding_model=models.embedding)


@dataclass(frozen=True, slots=True)
class ReembedPlan:
    """What an operator reads before deciding, and what the admin route serialises."""

    pipeline: ConfiguredPipeline
    totals: StalePipelineTotals
    documents: tuple[StalePipelineDocument, ...]


async def plan_reembed(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID | None = None,
    limit: int = DEFAULT_PAGE,
    offset: int = 0,
    settings: Settings | None = None,
    pipeline: ConfiguredPipeline | None = None,
) -> ReembedPlan:
    """Enumerate documents whose live version was built by a different pipeline.

    The totals cover the **whole** stale set while `documents` is one page of it, because the
    decision an operator is making is about the total and the action they can take is about
    the page. Reporting only the page would be R-80(4)'s failure in a different costume: a
    number inside a table beats a caveat above it, so the number inside the table has to be
    the one that answers the question being asked.
    """
    settings = settings or get_settings()
    # `pipeline` is supplied only to ask a hypothetical — "what would be stale if the
    # embedding slot pointed at *this* id" — which is how `tools.set_model` prices a flip
    # before making it. Every other caller wants the pipeline actually in force.
    pipeline = pipeline or await resolved_pipeline(session, settings)
    repo = DocumentChunkRepository(session)
    fingerprint_inputs = {
        "embedding_model": pipeline.embedding_model,
        "chunking_version": pipeline.chunking_version,
        "preprocessing_version": pipeline.preprocessing_version,
    }
    totals = await repo.stale_pipeline_totals(**fingerprint_inputs, owner_id=owner_id)
    documents = await repo.stale_pipeline_documents(
        **fingerprint_inputs,
        owner_id=owner_id,
        limit=min(max(limit, 1), MAX_PAGE),
        offset=max(offset, 0),
    )
    return ReembedPlan(pipeline=pipeline, totals=totals, documents=tuple(documents))


async def is_stale(
    session: AsyncSession, document_id: uuid.UUID, *, settings: Settings | None = None
) -> bool:
    """Whether this document's live version was built by the configured pipeline.

    R-84(3)'s precondition. `rebuild_document` calls it and refuses when it is false, which is
    the whole difference between this trigger and widening `/retry` to `ACTIVE` documents: a
    re-embed that will change nothing is not a cheaper re-embed, it is a full-price one whose
    result is byte-identical.
    """
    pipeline = await resolved_pipeline(session, settings)
    return await DocumentChunkRepository(session).is_pipeline_stale(
        document_id,
        embedding_model=pipeline.embedding_model,
        chunking_version=pipeline.chunking_version,
        preprocessing_version=pipeline.preprocessing_version,
    )


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "ConfiguredPipeline",
    "ReembedPlan",
    "configured_pipeline",
    "is_stale",
    "plan_reembed",
    "resolved_pipeline",
]
