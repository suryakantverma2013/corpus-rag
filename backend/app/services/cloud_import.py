"""Cloud-drive import — the join between the provider adapter and the ingestion path.

T-214, FR-KBM-10, R-63(5). Deliberately thin, and the thinness *is* the requirement: an
import is a **one-time copy** into the existing pipeline, so this module's whole job is to
turn a file id into a byte source and hand it to `upload_document`. Everything that makes an
upload safe and correct — FR-KBM-08 dedup, the FR-ERR-02 quota, the 50 MB ceiling, magic-byte
sniffing, the R-24 processing lock, R-32 scanning in the worker — happens there, to imported
bytes, because they go through the same function rather than a parallel one.

What is *not* here is as important as what is. No new document status, no `imported` flag, no
provenance column, no re-ingestion hook: an imported document is indistinguishable from an
uploaded one by design (R-63), and a schema change would contradict that as well as the
ruling's "no schema change, no migration".

The order of operations carries R-63(6)(2): metadata **first**, so an unsupported format or an
over-sized file is refused before a byte is transferred, and only then the stream — which is
capped regardless, in two independent places, because a declared size is the provider's claim.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keycloak_client import AccountNotLinkedError, KeycloakClient
from app.config import Settings
from app.db.models.users import User
from app.services import drive
from app.services.cloud_links import CloudProvider
from app.services.documents import UploadOutcome, UploadScope, upload_document
from app.services.jobs import JobQueue
from app.services.object_storage import ObjectStorage

log = structlog.get_logger(__name__)

__all__ = ["AccountNotLinkedError", "import_document"]


async def brokered_token(*, access_token: str, kc: KeycloakClient, settings: Settings) -> str:
    """The provider's live access token for this caller, from Keycloak (R-63(1)).

    Fetched per request and never stored. Raising `AccountNotLinkedError` straight through is
    intentional: FR-AUT-11 makes "not linked" the ordinary state of every user who has never
    asked for Drive, so it is the route's to render as an affordance, not this module's to
    dress up as a failure.
    """
    data = await kc.broker_token(alias=settings.keycloak.google_idp_alias, user_token=access_token)
    return str(data["access_token"])


async def import_document(
    *,
    provider: CloudProvider,
    file_id: str,
    scope: UploadScope,
    conversation_id: uuid.UUID | None,
    user: User,
    access_token: str,
    kc: KeycloakClient,
    session: AsyncSession,
    storage: ObjectStorage,
    queue: JobQueue,
    settings: Settings,
) -> UploadOutcome:
    """Copy one provider file into the caller's knowledge base (FR-KBM-10).

    Returns exactly what an upload returns — including the FR-KBM-08 duplicate — because the
    caller is the same function. R-33's response contract is reused rather than restated.
    """
    token = await brokered_token(access_token=access_token, kc=kc, settings=settings)

    # R-63(6)(2), the pre-download half. `fetch_metadata` validates the id (R-63(6)(1)),
    # refuses a format outside FR-ING-02, and refuses a declared size over the ceiling — all
    # before a byte of content moves.
    metadata = await drive.fetch_metadata(file_id=file_id, access_token=token, settings=settings)

    download = await drive.open_download(metadata=metadata, access_token=token, settings=settings)
    try:
        outcome = await upload_document(
            upload=download,
            scope=scope,
            conversation_id=conversation_id,
            user=user,
            session=session,
            storage=storage,
            queue=queue,
            settings=settings,
        )
    finally:
        # The connection is released whether the ingestion path accepted the bytes or refused
        # them — and it refuses often by design (duplicate, quota, unsupported), so this is the
        # ordinary path, not the exceptional one.
        await download.aclose()

    log.info(
        "cloud.import_completed",
        provider=provider.value,
        document_id=str(outcome.document_id),
        duplicate=outcome.duplicate,
        # Never the file name and never the id: one is user content and the other is a handle
        # to it in a third party's namespace.
    )
    return outcome
