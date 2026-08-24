"""The deletion task — purge storage, drop the vectors, mark `DELETED` (T-208, FR-ING-05).

The asynchronous half of FR-ING-05. The synchronous half already ran in
`app.services.documents.request_deletion`: by the time this task starts, the document is
`DELETE_PENDING` with `searchable = false` **committed**, so it has already left retrieval
(`_access_predicates` filters that column) no matter how long the purge takes or how often
it retries. Nothing here is on a user's critical path.

Two orderings are load-bearing.

**Objects are purged before the database transaction that marks `DELETED`** (R-39(9)). That
commit is what makes the deletion final and user-visible, so it must not claim the document
is gone while its bytes are still in the bucket. A crash between the purge and the commit is
harmless — `delete_prefix` is idempotent and the retry re-runs it — whereas the reverse
ordering leaves a `DELETED` document whose objects nothing will ever collect, since this
design has no storage sweeper.

**The chunk delete and the `DELETED` mark share one transaction**, for the reason R-36(4)
was amended: a reader must never see a half-purged document.

A purge that exhausts its retries **never writes `FAILED`** (R-39(7)). That state means
*ingestion* terminally failed, FR-KBM-04 renders it as `Failed`, and FR-KBM-07 pairs it with
a Retry affordance that would re-*ingest* the document the user asked to delete. The row
stays `DELETING`, still non-searchable, with the diagnostics on the job — and a repeated
`DELETE` re-drives it (R-39(2)).

Retry, backoff and dead-lettering come from `workers.common`, which records why arq
provides none of them.
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
from app.db.models.knowledge_job import KnowledgeJob
from app.db.repositories.chunks import DocumentChunkRepository
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.jobs import DOCUMENT_DELETED, KnowledgeJobRepository
from app.db.session import get_sessionmaker
from app.services.object_storage import (
    ObjectStorage,
    ObjectStorageError,
    document_prefix,
    get_object_storage,
)
from workers.common import backoff, is_final_attempt

log = structlog.get_logger(__name__)

# Stage milestones, matching the ingestion task's granularity.
_PROGRESS_RUNNING = 5
_PROGRESS_PURGED = 60
_PROGRESS_DONE = 100

_CODE_STORAGE_UNAVAILABLE = "OBJECT_STORAGE_UNAVAILABLE"
_CODE_UNEXPECTED = "DELETE_FAILED"


@dataclass(slots=True)
class _Deps:
    """What the deletion task needs. Populated by `workers.main`'s `on_startup`.

    Narrower than the ingestion task's: no embedder and no scanner, because deletion neither
    reads the file's contents nor produces vectors.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    storage: ObjectStorage
    settings: Settings

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> _Deps:
        return cls(
            sessionmaker=ctx.get("sessionmaker") or get_sessionmaker(),
            storage=ctx.get("storage") or get_object_storage(),
            settings=ctx.get("settings") or get_settings(),
        )


async def delete_document(ctx: dict[str, Any], document_id: str, job_id: str) -> None:
    """Purge one document (FR-ING-05).

    Same wire contract as `ingest_document`: two positional strings, IDs only.
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
        # Snapshot the key components before any rollback expires the ORM objects.
        prefix = document_prefix(
            tenant_id=document.tenant_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
        )

        try:
            await _run(
                session,
                deps=deps,
                document=document,
                job=job,
                prefix=prefix,
                logger=logger,
            )
        except Exception as exc:
            await session.rollback()
            try:
                await _handle_failure(
                    session,
                    deps=deps,
                    document_id=doc_uuid,
                    job_id=job_uuid,
                    exc=exc,
                    job_try=job_try,
                    logger=logger,
                )
            except Retry:
                raise
            except Exception:
                # The bookkeeping itself failed — the database is almost certainly what
                # broke. Convert to a retry so the job does not sit `RUNNING` forever.
                logger.exception("delete.bookkeeping_failed", original_error=str(exc))
                if not is_final_attempt(job_try, settings=deps.settings):
                    raise Retry(defer=backoff(job_try, settings=deps.settings)) from None
                raise


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
        logger.error("delete.rows_missing", document_found=document is not None)
        return None

    if job.status is JobStatus.SUCCEEDED:
        logger.info("delete.skipped", reason="job already succeeded")
        return None

    if document.status is DocumentStatus.DELETED:
        # FR-ING-04's named deletion short-circuit: "a `DELETED` document short-circuits
        # deletion". Reached by an arq redelivery, or by a repeat `DELETE` that raced the
        # worker finishing.
        logger.info("delete.skipped", reason="document already deleted")
        await jobs.update_status(job, JobStatus.SUCCEEDED, progress=_PROGRESS_DONE)
        await session.commit()
        return None

    await jobs.increment_attempt(job)
    await jobs.update_status(job, JobStatus.RUNNING, progress=_PROGRESS_RUNNING)
    # The document may still be `DELETE_PENDING` from the request, or `DELETING` from an
    # interrupted earlier attempt; either way this is the state it belongs in while the
    # purge runs. `searchable` was already false before the `202`.
    await documents.set_status(document, DocumentStatus.DELETING)
    await session.commit()
    return document, job


async def _run(
    session: AsyncSession,
    *,
    deps: _Deps,
    document: Document,
    job: KnowledgeJob,
    prefix: str,
    logger: Any,
) -> None:
    # --- purge object storage (no transaction held across the network) -------------------
    # The whole document prefix: every version's original and every derived artifact. Unlike
    # T-207's malware purge, which deletes only the *version* prefix because a superseded
    # version is still serving, nothing here survives.
    removed = await deps.storage.delete_prefix(prefix)
    logger.info("delete.objects_purged", prefix=prefix, objects_removed=removed)

    await KnowledgeJobRepository(session).update_status(
        job, JobStatus.RUNNING, progress=_PROGRESS_PURGED
    )
    await session.commit()

    # --- the terminal transaction --------------------------------------------------------
    chunks_removed = await DocumentChunkRepository(session).delete_by_document(document.id)
    # FR-ING-09's half of the same sentence (R-94(5)): the rasters went with the prefix above,
    # and these rows are the part `delete_prefix` cannot reach. In this transaction rather than
    # beside it, because a figure row outliving the document it describes is a URL that resolves
    # to nothing — and unlike the ingest path there is no later version to collect it.
    figures_removed = await DocumentFigureRepository(session).delete_by_document(document.id)
    await DocumentRepository(session).mark_deleted(document)
    # Defence in depth for §11's "delete while ingestion is running": the request already
    # superseded any open ingest job, but one enqueued in the gap between that commit and
    # this one would otherwise stay `QUEUED` forever against a document that no longer has
    # bytes to ingest.
    jobs = KnowledgeJobRepository(session)
    for open_job in await jobs.list_open_for_document(document.id, job_type=JobType.INGEST):
        await jobs.update_status(
            open_job,
            JobStatus.FAILED,
            error_code=DOCUMENT_DELETED,
            error_message="superseded: the document was deleted",
        )
    await jobs.update_status(job, JobStatus.SUCCEEDED, progress=_PROGRESS_DONE, clear_error=True)
    await session.commit()

    logger.info(
        "delete.completed",
        chunks_removed=chunks_removed,
        figures_removed=figures_removed,
        objects_removed=removed,
    )


async def _handle_failure(
    session: AsyncSession,
    *,
    deps: _Deps,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    exc: BaseException,
    job_try: int,
    logger: Any,
) -> None:
    """Record the failure on the **job**; never on the document (R-39(7))."""
    # Re-load: `rollback()` expired the identity map, so touching the caller's ORM objects
    # would fire an implicit lazy refresh — a `MissingGreenlet` under asyncio, masking the
    # real error. Same trap T-207 hit.
    job = await KnowledgeJobRepository(session).get(job_id)
    if job is None:
        logger.error("delete.job_vanished_during_failure_handling")
        return

    code = _classify(exc)
    message = f"{type(exc).__name__}: {exc}"

    if is_final_attempt(job_try, settings=deps.settings):
        # arq's `max_tries` check fires before the coroutine is invoked, so this is the last
        # time this code can run for this job — write the dead-letter now (NFR-REL-01).
        # The document stays `DELETING`: still non-searchable, still re-drivable by another
        # `DELETE`, and never rendered to the user as a failure they cannot act on.
        logger.error("delete.dead_letter", error_code=code, error=message, attempts=job_try)
        await KnowledgeJobRepository(session).update_status(
            job,
            JobStatus.DEAD_LETTER,
            error_code=code,
            error_message=f"{message} (dead-lettered after {job_try} attempts)",
        )
        await session.commit()
        return

    defer = backoff(job_try, settings=deps.settings)
    logger.warning("delete.retrying", error_code=code, error=message, defer_seconds=round(defer, 1))
    await KnowledgeJobRepository(session).update_status(
        job, JobStatus.QUEUED, error_code=code, error_message=message
    )
    await session.commit()
    raise Retry(defer=defer)


def _classify(exc: BaseException) -> str:
    """Map an exception to an `error_code`. Everything here is retryable.

    Unlike ingestion, deletion has no terminal failure mode: it neither parses the file nor
    calls an embedding API, so there is no malformed input to reject. A missing object is
    not an error at all — `delete_prefix` treats an absent key as done — and every other
    failure is the database or object storage being temporarily unavailable. The dead-letter
    path bounds how long that optimism lasts.
    """
    if isinstance(exc, ObjectStorageError):
        return _CODE_STORAGE_UNAVAILABLE
    return _CODE_UNEXPECTED
