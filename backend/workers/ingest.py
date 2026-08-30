"""The ingestion task — screen → parse → chunk → embed → swap (T-207, FR-ING-01/03/04).

Everything this task calls already exists and is tested; what belongs *here*, and nowhere
else, is the part each of those modules deliberately left undone:

* the FR-ING-01 state walk, and **the commit that is R-36(3)'s swap** — `plan_chunk_set`
  and `persist_chunk_set` never commit and never touch `documents` or `knowledge_jobs`;
* advancing **`documents.current_version`** inside that swap (R-37(9)). T-206's retrieval
  filters `document_chunks.document_version = documents.current_version`, and nothing
  upstream moves the pointer. Miss it and a re-ingested document reaches `ACTIVE`, lists in
  the KB modal, and matches nothing — silently;
* the R-31/R-32 malware screen, at the head, **before `PARSING`**;
* FR-ING-04's retry, backoff and dead-letter path;
* **the swap guard** (R-39(8)) — a re-read of the document under `FOR UPDATE` immediately
  before `mark_active`. `_begin`'s deletion short-circuit ran minutes earlier, before the
  parse and the embedding calls, so a `DELETE` that arrived since then is invisible to it
  and the swap would commit `searchable = True` over a deletion in progress.

**arq does not retry on a plain exception, and cannot report its own dead-letter.**
Both facts are load-bearing here and neither is obvious from arq's docs. Reading
`arq/worker.py`: a generic exception sets ``finish=True`` and the job is *dropped* — only
``Retry``, ``RetryJob`` and ``CancelledError`` re-queue. And the ``max_tries`` test happens
at the *top* of ``run_job``, so once ``job_try > max_tries`` arq finishes the job **without
invoking this coroutine at all**. So NFR-REL-01's "automatic retry, backoff, and
dead-letter handling" is built explicitly: retryable failures compute their own backoff and
raise ``Retry(defer=…)``, and the task detects its own final attempt to write
``JobStatus.DEAD_LETTER`` before that ever happens.

**A retryable failure leaves the document in its in-flight state** (R-38(5)). FR-ING-01 has
no "retrying" state, and flapping `EMBEDDING → FAILED → PARSING` across three attempts would
churn the FR-KBM-04 badge and the T-210 SSE stream for something the user cannot act on.
`FAILED` means *terminally* failed. The retry story lives on `knowledge_jobs`
(`attempt_count`, `error_code`, `error_message`) — which is exactly what §11's "preserve job
diagnostics and retry state" asks for and what FR-ING-06's `GET /jobs/{id}` will render.

Transactions are short and each stage commits before any network call. The single exception
is the swap, which is one transaction by requirement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from arq.worker import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.document_figure import DocumentFigure
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.jobs import DOCUMENT_DELETED, KnowledgeJobRepository
from app.db.session import get_sessionmaker
from app.ingestion.chunker import chunk_document
from app.ingestion.figures import ExtractedFigure, extract_figures
from app.ingestion.incremental import persist_chunk_set, plan_chunk_set
from app.ingestion.parsers import parse_document
from app.ingestion.scanner import Scanner, ScanResult, build_scanner
from app.services import audit
from app.services.embeddings import EmbeddingClient, get_embedding_client
from app.services.model_selection import resolve_models
from app.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageError,
    artifact_key,
    document_version_prefix,
    get_object_storage,
)
from workers.common import backoff, is_final_attempt

log = structlog.get_logger(__name__)

# Progress milestones for FR-ING-06 / the FR-KBM-09 SSE stream. Coarse on purpose: they
# mark stage boundaries, and no stage reports partial progress.
_PROGRESS_RUNNING = 5
_PROGRESS_PARSING = 25
_PROGRESS_CHUNKING = 45
_PROGRESS_EMBEDDING = 65
_PROGRESS_INDEXING = 85
_PROGRESS_DONE = 100

# Reason codes this task owns (the others come from the parser/embedding/scanner trees, and
# `DOCUMENT_DELETED` is shared with the deletion request — see `repositories.jobs`).
_CODE_OBJECT_MISSING = "OBJECT_MISSING"
_CODE_STORAGE_UNAVAILABLE = "OBJECT_STORAGE_UNAVAILABLE"
_CODE_UNEXPECTED = "INGEST_FAILED"
_CODE_EMPTY_CHUNK_SET = "NO_CHUNKS_PRODUCED"

# Terminal document states that short-circuit ingestion (FR-ING-04).
_DELETION_STATES = frozenset(
    {DocumentStatus.DELETE_PENDING, DocumentStatus.DELETING, DocumentStatus.DELETED}
)


class _MalwareDetected(Exception):
    """Internal signal: a `ScanResult` came back INFECTED. Never leaves this module."""

    def __init__(self, result: ScanResult) -> None:
        super().__init__(result.detail or "malware screening failed")
        self.result = result


class _EmptyChunkSet(Exception):
    """A parse that yielded no chunks. Terminal — a retry re-parses the same bytes."""

    code = _CODE_EMPTY_CHUNK_SET
    retryable = False


class _DocumentDeleted(Exception):
    """A deletion landed after `_begin` passed. Terminal — the document is going away."""

    code = DOCUMENT_DELETED
    retryable = False


@dataclass(slots=True)
class _Deps:
    """What the task needs from the world. Populated by `workers.main`'s `on_startup`.

    Resolved from `ctx` with a fall-back to the process-wide getters so the task is
    directly callable in tests with a hand-built ctx, and so a worker started without the
    startup hook still works rather than failing obscurely.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    storage: ObjectStorage
    embedder: EmbeddingClient
    scanner: Scanner
    settings: Settings

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> _Deps:
        settings = ctx.get("settings") or get_settings()
        return cls(
            sessionmaker=ctx.get("sessionmaker") or get_sessionmaker(),
            storage=ctx.get("storage") or get_object_storage(),
            embedder=ctx.get("embedder") or get_embedding_client(),
            scanner=ctx.get("scanner") or build_scanner(settings),
            settings=settings,
        )


async def ingest_document(ctx: dict[str, Any], document_id: str, job_id: str) -> None:
    """Ingest one document version.

    Signature is the wire contract from T-202: two positional strings, matching
    ``pool.enqueue_job(INGEST_TASK_NAME, str(document_id), str(job_id))``. **IDs only,
    never bytes** — the worker re-reads the row and streams the object itself, so a job
    sitting on the broker stays small and cannot go stale against the database.
    """
    deps = _Deps.from_ctx(ctx)
    job_try = int(ctx.get("job_try", 1))
    doc_uuid = uuid.UUID(document_id)
    job_uuid = uuid.UUID(job_id)

    logger = log.bind(document_id=document_id, job_id=job_id, job_try=job_try)

    async with deps.sessionmaker() as session:
        prepared = await _begin(session, document_id=doc_uuid, job_id=job_uuid, logger=logger)
        if prepared is None:
            return
        document, job = prepared
        target_version = job.document_version
        # Snapshot the scalars the pipeline needs; the ORM objects belong to `session`,
        # and the plan below deliberately runs on a different one.
        context = _DocumentContext.of(document)

        try:
            await _run(
                session,
                deps=deps,
                document=document,
                job=job,
                context=context,
                target_version=target_version,
                logger=logger,
            )
        except Exception as exc:
            await session.rollback()
            try:
                await _handle_failure(
                    session,
                    deps=deps,
                    context=context,
                    job_id=job_uuid,
                    target_version=target_version,
                    exc=exc,
                    job_try=job_try,
                    logger=logger,
                )
            except Retry:
                raise
            except Exception:
                # The bookkeeping itself failed — almost always the database being the
                # thing that broke. Without this the job would sit `RUNNING` forever, and
                # nothing sweeps that state. Convert to a retry so it gets another attempt
                # once the database is back.
                logger.exception("ingest.bookkeeping_failed", original_error=str(exc))
                if not is_final_attempt(job_try, settings=deps.settings):
                    raise Retry(defer=backoff(job_try, settings=deps.settings)) from None
                raise


@dataclass(frozen=True, slots=True)
class _DocumentContext:
    """Scalars copied off the ORM object so the pipeline never re-reads a detached row."""

    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    filename: str
    storage_uri: str

    @classmethod
    def of(cls, document: Document) -> _DocumentContext:
        return cls(
            document_id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            tenant_id=document.tenant_id,
            owner_id=document.owner_id,
            filename=document.filename,
            storage_uri=document.storage_uri,
        )


# ---- stages -------------------------------------------------------------------------


async def _begin(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    logger: Any,
) -> tuple[Document, KnowledgeJob] | None:
    """Load the rows and apply the FR-ING-04 short-circuits. None means "nothing to do"."""
    documents = DocumentRepository(session)
    jobs = KnowledgeJobRepository(session)

    document = await documents.get(document_id)
    job = await jobs.get(job_id)
    if document is None or job is None:
        # Nothing to fail: the rows this job names do not exist. Retrying cannot conjure
        # them, and there is no row on which to record a diagnostic.
        logger.error("ingest.rows_missing", document_found=document is not None)
        return None

    if job.status is JobStatus.SUCCEEDED:
        # A replay of an already-finished job — arq redelivery, or the sweeper racing a
        # recovered broker. FR-ING-04 says this must be safe, not merely tolerated.
        logger.info("ingest.skipped", reason="job already succeeded")
        return None

    if document.deleted_at is not None or document.status in _DELETION_STATES:
        # §11 "delete while ingestion is running": the deletion path already set
        # `searchable = false` synchronously (FR-ING-05). Ingesting now would resurrect a
        # document the user deleted, so abandon the job and — critically — leave
        # `documents.status` alone rather than dragging it back out of the deletion flow.
        logger.info("ingest.abandoned", reason="document is being deleted")
        await jobs.update_status(
            job,
            JobStatus.FAILED,
            error_code=DOCUMENT_DELETED,
            error_message="document was deleted before ingestion ran",
        )
        await session.commit()
        return None

    already_built = document.current_version >= job.document_version
    if document.status is DocumentStatus.ACTIVE and already_built:
        # FR-ING-04's named short-circuit: an ACTIVE document at this version or later is
        # already ingested. Compared with `>=` rather than `==` so a redelivered v1 job
        # cannot roll a document that has since been replaced back to v1.
        logger.info("ingest.skipped", reason="document already active at this version")
        await jobs.update_status(job, JobStatus.SUCCEEDED, progress=_PROGRESS_DONE)
        await session.commit()
        return None

    await jobs.increment_attempt(job)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_RUNNING)
    await session.commit()
    return document, job


async def _run(
    session: AsyncSession,
    *,
    deps: _Deps,
    document: Document,
    job: KnowledgeJob,
    context: _DocumentContext,
    target_version: int,
    logger: Any,
) -> None:
    documents = DocumentRepository(session)
    jobs = KnowledgeJobRepository(session)

    # --- fetch + screen (R-31: before PARSING, document still QUEUED) -----------------
    # One read, reused by the scan and the parse. Bounded by FR-ERR-01's 50 MB ceiling,
    # which is also why the whole payload can be held in memory.
    key = deps.storage.key_for_uri(context.storage_uri)
    payload = await deps.storage.get(key)

    result = await deps.scanner.scan(payload, filename=context.filename)
    if result.infected:
        raise _MalwareDetected(result)

    # --- parse ------------------------------------------------------------------------
    await documents.set_status(document, DocumentStatus.PARSING)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_PARSING)
    await session.commit()

    parsed = await parse_document(payload, filename=context.filename)

    # --- chunk ------------------------------------------------------------------------
    await documents.set_status(document, DocumentStatus.CHUNKING)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_CHUNKING)
    await session.commit()

    # `embedding_model` is passed explicitly, never defaulted, and since T-612 it is the
    # **resolved** id rather than the client's own: an operator may repoint
    # `ModelSlot.EMBEDDING` between jobs, and the chunker would otherwise read
    # `OPENAI_EMBEDDING_MODEL` while the embed call used the override. Resolved here, on a
    # session that is between transactions (the commit above just landed) and per job rather
    # than per process, because the worker outlives any number of operator changes.
    #
    # From here the id travels on `chunked` alone — `plan_chunk_set` embeds with
    # `chunked.embedding_model` — so the fingerprint and the vector cannot name different
    # models without someone editing two files to make it so.
    embedding_model = (await resolve_models(session, deps.settings)).embedding
    chunked = await chunk_document(parsed, embedding_model=embedding_model)
    if not chunked.chunks:
        # Defence in depth: R-34 makes the parsers raise `NoExtractableTextError` rather
        # than yield nothing, so reaching here is a bug — but a zero-chunk `ACTIVE`
        # document would list as ready and never answer, which is worse than a clear fail.
        raise _EmptyChunkSet(f"{context.filename} produced no chunks")

    # --- embed (no transaction held across the network) --------------------------------
    await documents.set_status(document, DocumentStatus.EMBEDDING)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_EMBEDDING)
    await session.commit()

    plan = await _plan_on_a_disposable_session(
        deps=deps, chunked=chunked, context=context, target_version=target_version
    )

    # --- index + swap ------------------------------------------------------------------
    await documents.set_status(document, DocumentStatus.INDEXING)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_INDEXING)
    await session.commit()

    # THE SWAP GUARD (R-39(8)). `_begin`'s deletion short-circuit ran before the parse, the
    # scan and the embedding calls — minutes ago for a large document — so a `DELETE` that
    # arrived since then is invisible to it. Without this, `mark_active` below commits
    # `searchable = True` and a version bump over a deletion in progress, resurrecting the
    # document; and if the purge has already run, it resurrects it as an `ACTIVE` row whose
    # original bytes are gone.
    #
    # `FOR UPDATE` opens the swap transaction by locking the row, so the check and the
    # commit cannot be interleaved — the delete request takes the same lock and blocks.
    #
    # **The DELETE-job check is not belt and braces.** The state writes above operate on an
    # ORM instance loaded before the embed window, so `set_status(INDEXING)` on the previous
    # line has already overwritten any `DELETE_PENDING` a concurrent request wrote — the
    # status field alone reads clean here, which is exactly how this guard failed its first
    # test. A deletion job is never deleted, so its existence is the one signal the
    # ingestion path cannot erase.
    document = await documents.get_for_update(context.document_id)
    deleting = document is None or document.deleted_at is not None
    deleting = deleting or (document is not None and document.status in _DELETION_STATES)
    deleting = deleting or await jobs.has_job(context.document_id, job_type=JobType.DELETE)
    if deleting:
        raise _DocumentDeleted(f"{context.document_id} entered the deletion path mid-ingest")

    # Read under the lock and before `mark_active` overwrites it: the version whose objects
    # become redundant once the swap below is durable (R-40(3)). It must come from the
    # `get_for_update` result — `populate_existing` makes that read authoritative, while the
    # instance this task has been carrying predates the whole embed window.
    previous_version = document.current_version

    # THE STALE-JOB GUARD (R-40(4)). `_begin`'s `current_version >= document_version`
    # short-circuit closes the *redelivery* case, where a newer version had already won
    # before this job started. It cannot close the *concurrent* case: a replace can commit
    # its own swap while this job sits between `_begin` and here — minutes, for a large
    # document. Committing now would roll the pointer backwards, and `persist_chunk_set`'s
    # `delete_other_versions(keep_version=target_version)` would delete the newer version's
    # rows outright, leaving an ACTIVE document whose only chunks are the *new* bytes
    # labelled with the *old* version number — which retrieval then serves silently, since
    # it matches on `document_version = current_version`.
    #
    # **Strictly greater, not `>=`.** `_begin` can use `>=` because it pairs the test with
    # `status is ACTIVE`; here there is no such pairing, and equality is the *normal* case
    # twice over — a first ingest targets v1 against the column's server default of 1, and
    # an FR-ING-04 same-version retry targets exactly the version already recorded. Both
    # must proceed. Only a document that has moved *past* this job's target has been
    # overtaken.
    #
    # `SUCCEEDED` is the correct outcome: someone newer won and nothing is wrong.
    if previous_version > target_version:
        logger.info(
            "ingest.skipped",
            reason="a newer version was swapped in mid-job",
            version=target_version,
            current_version=previous_version,
        )
        await jobs.update_status(
            job, JobStatus.SUCCEEDED, progress=_PROGRESS_DONE, clear_error=True
        )
        await session.commit()
        return

    persisted = await persist_chunk_set(session, plan=plan)
    await documents.mark_active(
        document,
        chunk_count=persisted.total,
        current_version=target_version,  # R-37(9) — in this transaction, not after it
        page_count=parsed.page_count,
        text_quality_ratio=parsed.text_quality_ratio,  # FR-ING-10
    )
    await jobs.update_status(job, JobStatus.SUCCEEDED, progress=_PROGRESS_DONE, clear_error=True)
    # THE SWAP (R-36(3)). Chunk rows, the version pointer, `searchable`, the document state
    # and the job outcome all land together: readers see the previous version until this
    # commit and only the new one after it.
    await session.commit()

    # R-40(3): purge the superseded version's objects **after** the swap commit —
    # deliberately the reverse of R-39(9)'s purge-then-commit ordering, and for the
    # symmetric reason. In a deletion the commit is what makes the bytes unreachable, so
    # purging after it orphans them. Here the commit is what makes them *redundant*: until
    # it lands, v(previous) is still the document's live content, still serving retrieval,
    # and still the only thing a rolled-back swap can fall back on. Purging first and then
    # failing to commit would destroy the original of the version that is still answering
    # questions, from which nothing can rebuild.
    #
    # FR-ING-09 (T-714). **After the swap, before the purge, and it can never fail the job.**
    #
    # After the swap for two reasons. A figure is presentation data (R-94(4)), so nothing about
    # one may delay a document reaching `ACTIVE`; and the writes are storage `put`s, which
    # cannot go inside the swap at all — R-36(4)'s justification for moving the collect step
    # *into* that transaction rests on it holding no network I/O.
    #
    # Before the purge so the ordering matches the rest of this function: the old version's
    # figure rows are replaced while its objects still exist, rather than after they are gone.
    await _store_figures(
        session,
        deps=deps,
        context=context,
        payload=payload,
        target_version=target_version,
        logger=logger,
    )

    # Outside the transaction by construction: R-36(4)'s justification for moving the
    # collect step *inside* the swap rests on that transaction holding no network I/O.
    await _purge_superseded_versions(
        deps=deps,
        context=context,
        first=previous_version,
        last=target_version,
        logger=logger,
    )

    logger.info(
        "ingest.completed",
        version=target_version,
        chunks=persisted.total,
        reused=persisted.reused,
        added=persisted.added,
        embedded_inputs=plan.embedded_inputs,
        collected=persisted.collected,
    )


async def _plan_on_a_disposable_session(
    *, deps: _Deps, chunked: Any, context: _DocumentContext, target_version: int
) -> Any:
    """Run `plan_chunk_set` on a session nothing else will ever use again.

    `plan_chunk_set` reads the stored fingerprints and *then* calls OpenAI, so its read
    transaction stays open across the embedding window — minutes, for a large document
    under a rate-limit storm. On a cluster with `idle_in_transaction_session_timeout` set
    (the default on most managed Postgres) that connection gets killed mid-embed.

    Giving it a throwaway session makes that harmless: the embeddings live in the returned
    plan, the session is never touched again, and the swap runs on the caller's session,
    which has been sitting outside a transaction the whole time. It also keeps the read
    snapshot's `xmin` pin off the connection that is about to do the writing.
    """
    async with deps.sessionmaker() as planning_session:
        return await plan_chunk_set(
            session=planning_session,
            client=deps.embedder,
            chunked=chunked,
            document_id=context.document_id,
            document_version=target_version,
            knowledge_base_id=context.knowledge_base_id,
            tenant_id=context.tenant_id,
        )


# ---- failure handling ---------------------------------------------------------------


async def _handle_failure(
    session: AsyncSession,
    *,
    deps: _Deps,
    context: _DocumentContext,
    job_id: uuid.UUID,
    target_version: int,
    exc: BaseException,
    job_try: int,
    logger: Any,
) -> None:
    # Re-load rather than reusing the caller's ORM objects. `rollback()` expires the whole
    # identity map, so touching an attribute on the old instances would fire an implicit
    # lazy refresh — which under asyncio is a `MissingGreenlet`, i.e. the error handler
    # would itself explode and mask the real failure.
    document = await DocumentRepository(session).get(context.document_id)
    job = await KnowledgeJobRepository(session).get(job_id)
    if document is None or job is None:
        logger.error("ingest.rows_vanished_during_failure_handling")
        return

    if isinstance(exc, _MalwareDetected):
        await _purge_and_fail(
            session,
            deps=deps,
            document=document,
            job=job,
            context=context,
            target_version=target_version,
            result=exc.result,
            logger=logger,
        )
        return

    if isinstance(exc, _DocumentDeleted):
        # The job failed, the document did not. Writing `FAILED` here would drag it back
        # out of the deletion flow, render it as `Failed` in FR-KBM-04, and offer a Retry
        # that re-ingests the document the user deleted — the same reasoning as R-39(7) and
        # as `_begin`'s deletion short-circuit, which likewise leaves `documents.status`
        # alone. The purge job owns this document's state from here.
        logger.info("ingest.abandoned", reason="document was deleted mid-ingest")
        await KnowledgeJobRepository(session).update_status(
            job,
            JobStatus.FAILED,
            error_code=DOCUMENT_DELETED,
            error_message=str(exc),
        )
        await session.commit()
        return

    code, retryable = _classify(exc)
    message = f"{type(exc).__name__}: {exc}"

    if not retryable:
        logger.warning("ingest.failed", error_code=code, error=message, terminal=True)
        await _fail(session, document=document, job=job, code=code, message=message)
        # Return normally. Raising would make arq log a second, spurious failure for a
        # document that is already correctly marked FAILED.
        return

    if is_final_attempt(job_try, settings=deps.settings):
        # The last attempt arq will ever run this task on. arq's own `max_tries` check
        # fires *before* the coroutine is invoked, so if we simply raised `Retry` here the
        # next delivery would be discarded without ever reaching this code and the job
        # would sit `RUNNING` forever. Write the dead-letter now (NFR-REL-01).
        logger.error("ingest.dead_letter", error_code=code, error=message, attempts=job_try)
        await _fail(
            session,
            document=document,
            job=job,
            code=code,
            message=f"{message} (dead-lettered after {job_try} attempts)",
            status=JobStatus.DEAD_LETTER,
        )
        return

    defer = backoff(job_try, settings=deps.settings)
    logger.warning("ingest.retrying", error_code=code, error=message, defer_seconds=round(defer, 1))
    # R-38(5): the job carries the diagnostics; the document keeps its in-flight state so
    # the KB badge does not flap between FAILED and the next stage on every attempt.
    await KnowledgeJobRepository(session).update_status(
        job, JobStatus.QUEUED, error_code=code, error_message=message
    )
    await session.commit()
    raise Retry(defer=defer)


async def _fail(
    session: AsyncSession,
    *,
    document: Document,
    job: KnowledgeJob,
    code: str,
    message: str,
    status: JobStatus = JobStatus.FAILED,
) -> None:
    """Terminal outcome: the document is FAILED and the job records why (FR-ING-01/06)."""
    await DocumentRepository(session).set_status(
        document, DocumentStatus.FAILED, error_message=message
    )
    await KnowledgeJobRepository(session).update_status(
        job, status, error_code=code, error_message=message
    )
    await session.commit()


async def _store_figures(
    session: AsyncSession,
    *,
    deps: _Deps,
    context: _DocumentContext,
    payload: bytes,
    target_version: int,
    logger: Any,
) -> None:
    """Extract, store and record this version's figures (FR-ING-09, R-94(5)). Never raises.

    **The collect runs whether or not anything was extracted, and that is the load-bearing
    part.** `replace_for_version` with an empty list is not a no-op: it says *this document has
    no figures*, which is exactly true when the feature is off, when the format has no pages,
    and when a detector found nothing. Skipping it in those cases would leave the previous
    version's rows behind, pointing at objects `_purge_superseded_versions` is about to delete —
    the dangling URL R-94(5) names, arrived at by turning a feature off.

    **Failure is silent by requirement, not by neglect.** FR-ING-09 fails open: the document is
    already `ACTIVE` and answering, and a storage blip or a MuPDF fault must not turn a
    successful ingestion into `Failed`. That is the same asymmetry R-88(9) settled for
    recognition, one stage further along — extraction produces the text a document *is*, this
    produces a picture beside it. The cost when it fires is one warning and no pictures.
    """
    figures: tuple[ExtractedFigure, ...] = ()
    try:
        # `deps.settings.parser`, never `get_settings()`: this task takes its configuration
        # from the arq context (`_Deps.from_ctx`), and a module that reached past it would be
        # unconfigurable from the one place the worker is configured.
        figures = await extract_figures(
            payload, filename=context.filename, limits=deps.settings.parser
        )
        rows = [
            await _store_one_figure(
                deps=deps, context=context, figure=figure, target_version=target_version
            )
            for figure in figures
        ]
        stored = await DocumentFigureRepository(session).replace_for_version(
            context.document_id, document_version=target_version, figures=rows
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — R-94: never fail an ACTIVE document for a picture
        await session.rollback()
        logger.warning(
            "ingest.figures_degraded",
            version=target_version,
            detected=len(figures),
            exc_info=True,
        )
        return

    if stored:
        logger.info("ingest.figures_stored", version=target_version, figures=stored)


async def _store_one_figure(
    *,
    deps: _Deps,
    context: _DocumentContext,
    figure: ExtractedFigure,
    target_version: int,
) -> DocumentFigure:
    """Put one raster under the version prefix and build its row.

    The key is the figure's **content hash**, not its ordinal, so two identical crops in one
    version share one object and a re-ingestion that produces the same crop overwrites it with
    the same bytes — which is what makes T-715's long cache lifetime honest (R-94(5)).

    Under `artifacts/` and inside `document_version_prefix`, which is the whole of the object
    half of R-36/R-39 parity: `delete_prefix` on `v{n}/` and on the document prefix already
    remove these, and always did. Nothing new purges them because nothing new has to.
    """
    key = artifact_key(
        tenant_id=context.tenant_id,
        knowledge_base_id=context.knowledge_base_id,
        document_id=context.document_id,
        version=target_version,
        name=f"figures/{figure.content_sha256}.png",
    )
    await deps.storage.put(key, figure.png, content_type="image/png")
    return DocumentFigure(
        document_id=context.document_id,
        document_version=target_version,
        page_number=figure.page_number,
        figure_index=figure.index,
        content_sha256=figure.content_sha256,
        storage_uri=deps.storage.uri_for(key),
        caption=figure.caption,
        bbox_x0=figure.x0,
        bbox_y0=figure.y0,
        bbox_x1=figure.x1,
        bbox_y1=figure.y1,
        width_px=figure.width_px,
        height_px=figure.height_px,
        byte_size=figure.byte_size,
    )


async def _purge_superseded_versions(
    *,
    deps: _Deps,
    context: _DocumentContext,
    first: int,
    last: int,
    logger: Any,
) -> None:
    """Best-effort delete of every version prefix in ``[first, last)`` (R-40(3)).

    A **range**, not just ``first``: a replace whose ingestion failed has already burned
    its version number, and its original — referenced by nothing, since `storage_uri` has
    since been repointed — would otherwise survive until the document is deleted.

    **``first < last`` is the guard that matters.** On a first ingest and on every
    FR-ING-04 same-version retry ``previous_version == target_version``, and without the
    range this would delete the prefix holding the original of the document that has just
    gone `ACTIVE` — silently, because nothing reads that object again until a retry, which
    then fails `OBJECT_MISSING` (terminal) on a document nothing can rebuild. `range()`
    makes that case a no-op rather than a catastrophe.

    Never raises. The swap is already committed and the document is `ACTIVE`; failing the
    job over an object-storage blip would render `Failed` for a document that works.
    """
    for version in range(first, last):
        prefix = document_version_prefix(
            tenant_id=context.tenant_id,
            knowledge_base_id=context.knowledge_base_id,
            document_id=context.document_id,
            version=version,
        )
        try:
            removed = await deps.storage.delete_prefix(prefix)
            logger.info("ingest.superseded_purged", version=version, objects_removed=removed)
        except Exception:  # noqa: BLE001 — the swap is durable; never fail the job for this
            logger.error(
                "ingest.superseded_purge_failed", version=version, prefix=prefix, exc_info=True
            )


async def _purge_and_fail(
    session: AsyncSession,
    *,
    deps: _Deps,
    document: Document,
    job: KnowledgeJob,
    context: _DocumentContext,
    target_version: int,
    result: ScanResult,
    logger: Any,
) -> None:
    """R-31(1): a detection fails the job **and purges the object**.

    The version prefix, not the whole document prefix — on a T-209 replace the superseded
    version is still serving retrieval and its original must survive. The document row
    survives too, with `status = FAILED` and a now-dangling `storage_uri`: this is a failed
    ingestion, not an FR-ING-05 deletion, and the KB surface must show the user why their
    upload was rejected.
    """
    prefix = document_version_prefix(
        tenant_id=context.tenant_id,
        knowledge_base_id=context.knowledge_base_id,
        document_id=context.document_id,
        version=target_version,
    )
    try:
        removed = await deps.storage.delete_prefix(prefix)
        logger.warning(
            "ingest.malware_purged",
            reason_code=result.reason_code,
            signature=result.signature,
            objects_removed=removed,
        )
    except ObjectStorageError as exc:
        # Do not let a storage blip keep the document out of FAILED — the row is the thing
        # the user sees, and leaving it mid-flight is worse than an orphaned object.
        logger.error("ingest.malware_purge_failed", error=str(exc), prefix=prefix)

    message = result.detail or "malware screening rejected this document"
    await DocumentRepository(session).set_status(
        document, DocumentStatus.FAILED, error_message=message
    )
    await KnowledgeJobRepository(session).update_status(
        job,
        JobStatus.FAILED,
        error_code=result.reason_code,
        error_message=message,
    )
    await audit.record_document_event(
        session,
        actor_id=context.owner_id,
        document_id=context.document_id,
        action="malware_purge",
    )
    await session.commit()


def _classify(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to `(error_code, retryable)`.

    T-203, T-205 and the scanner all expose `code` (and, where the answer is not uniform,
    `retryable`) as `ClassVar`s precisely so this stays a lookup rather than a growing
    isinstance ladder. `ParseError` deliberately has **no** `retryable`: every parse
    failure is terminal by R-34, so the `getattr` default of False is the correct reading,
    not an accident.
    """
    if isinstance(exc, ObjectNotFoundError):
        # The original is gone. A retry re-reads the same missing key forever.
        return _CODE_OBJECT_MISSING, False
    if isinstance(exc, ObjectStorageError):
        return _CODE_STORAGE_UNAVAILABLE, True

    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code, bool(getattr(exc, "retryable", False))

    # Unrecognised — a DB error, a bug, a transient anything. Retryable, because the
    # alternative is failing a document permanently on a blip; the dead-letter path bounds
    # how long that optimism lasts.
    return _CODE_UNEXPECTED, True
