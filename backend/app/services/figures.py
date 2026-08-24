"""Serving a document's own figures (NFR-SEC-10, T-715, R-94(6)).

**Why this is admissible at all, given R-31.** R-31 deferred malware scanning *because* Corpus
never re-serves an uploaded file, and FR-CIT-05 was declined on the same ground; its revisit
trigger said that adding such a surface makes a real scanner required, and R-32 shipped one. The
trigger is discharged twice over here, because what this serves **is not the file**: it is a
raster the ingestion pipeline re-encoded from a rendered page region, so no macro, script,
embedded file or form action of the original can survive into it — a property no amount of
scanning the original would give. **The uploaded file itself remains unservable by any route**,
which is why FR-CIT-05 stays declined for its own subject.

**Every refusal is the same 404.** A wrong id, a foreign document, a deleted one, one mid-replace
and an object that has gone missing all raise `FigureNotFoundError`. Distinguishing them would
make the route a probe for which documents exist and who owns them (NFR-SEC-02), and the client
has one behaviour for all of them anyway: FR-CIT-07 says a citation with no figure renders exactly
as it does today.

**A missing object is a 404 and never a 500.** The row and its raster are written in one
transaction after one `put`, so a row without bytes should not arise — but "should not arise" is
the wrong thing for a route to assume about object storage, and the failure it would otherwise
produce is a 500 on a page that is merely missing a picture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.figures import DocumentFigureRepository
from app.services.object_storage import ObjectNotFoundError, ObjectStorage, ObjectStorageError

log = structlog.get_logger(__name__)

#: The one media type this serves. `render_figure` encodes PNG and nothing else, so this is a
#: statement about our own renderer rather than a guess about bytes — which is what makes
#: `nosniff` on the route an assertion rather than a hope.
FIGURE_MEDIA_TYPE = "image/png"


class FigureNotFoundError(Exception):
    """No figure this caller may be shown. Rendered `404`, whatever the reason."""


@dataclass(frozen=True, slots=True)
class ServedFigure:
    """The bytes and the two things the route needs to describe them."""

    png: bytes
    content_sha256: str
    byte_size: int


async def load_figure(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    document_id: uuid.UUID,
    content_sha256: str,
    owner_id: uuid.UUID,
) -> ServedFigure:
    """The figure ``content_sha256`` of ``document_id``, if this caller may be shown it.

    The authorization decision is `DocumentFigureRepository.get_servable`'s single query — it
    is not re-stated here, because two places that both decide who may see a figure is how one
    of them comes to be wrong.
    """
    figure = await DocumentFigureRepository(session).get_servable(
        document_id, content_sha256=content_sha256, owner_id=owner_id
    )
    if figure is None:
        raise FigureNotFoundError(f"no servable figure {content_sha256} for document {document_id}")

    try:
        png = await storage.get(storage.key_for_uri(figure.storage_uri))
    except ObjectNotFoundError as exc:
        # The row outlived its object: a purge that ran early, a restored database, a bucket
        # someone emptied. Logged because it is a real inconsistency an operator should see,
        # and answered 404 because the caller's page is simply missing a picture.
        log.warning(
            "figure.object_missing",
            document_id=str(document_id),
            content_sha256=content_sha256,
            storage_uri=figure.storage_uri,
        )
        raise FigureNotFoundError(str(exc)) from exc
    except ObjectStorageError:
        # Deliberately **not** converted to 404: storage being down is not "no such figure",
        # and a 404 would teach a client to stop asking. The route lets this become the 503
        # every other storage-backed route answers (R-33/R-40).
        raise

    return ServedFigure(png=png, content_sha256=figure.content_sha256, byte_size=len(png))
