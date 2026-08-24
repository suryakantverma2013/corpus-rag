"""`GET /documents/{id}/figures/{sha}` — NFR-SEC-10 (T-715, R-94(6)).

**The security assertions are the deliverable**, and three of them are the reason this file is
longer than the route:

* **an administrator gets 404.** Every sibling route under `/documents` widens for one under
  FR-USR-04, and this must not — its siblings disclose *management*, this discloses *content*.
  It is the single most likely thing for a later reader to "fix".
* **each refusal predicate is driven against the state only it catches.** `searchable` and
  `status == ACTIVE` overlap on almost every row, so testing them against one document would
  leave either free to be deleted; a document mid-replace is `searchable` and not `ACTIVE`, and
  that is the state that tells them apart.
* **the uploaded file is still unservable by any route.** R-31 is narrowed by NFR-SEC-10, not
  repealed, and the only way to state that is over the whole published contract rather than
  over this one handler.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.models.document_figure import DocumentFigure
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.services.object_storage import (
    LocalFilesystemStorage,
    ObjectStorageError,
    artifact_key,
    get_object_storage,
)

pytestmark = pytest.mark.usefixtures("patch_jwks")

REPO_ROOT = Path(__file__).resolve().parents[2]

API = "/api/v1/documents"


def _png() -> bytes:
    """A real 1x1 PNG, rendered by the library `render_figure` uses.

    Genuinely a PNG because the success case asserts the media type the route declares; a
    placeholder would make that assertion vacuous.
    """
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1, 1))
    # **`clear_with` is not tidiness.** A bare `Pixmap` allocates its sample buffer without
    # initialising it, so two calls encode different pixels and the "same bytes" assertion
    # below fails against a route that is working perfectly. `render_figure` never hits this:
    # `get_pixmap` fills every sample from the page.
    pixmap.clear_with(255)
    return pixmap.tobytes("png")


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "objects")


@pytest.fixture
def app(app, storage: LocalFilesystemStorage):  # noqa: ANN001, ANN201
    app.dependency_overrides[get_object_storage] = lambda: storage
    return app


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.test"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _figure(
    session: AsyncSession,
    storage: LocalFilesystemStorage,
    *,
    owner_id: uuid.UUID,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    searchable: bool = True,
    deleted: bool = False,
    current_version: int = 1,
    figure_version: int = 1,
    store_object: bool = True,
) -> tuple[Document, str]:
    """A document and one of its figures, with the raster really in the bucket."""
    from datetime import UTC, datetime

    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        mime_type="application/pdf",
        storage_uri=f"file:///objects/{uuid.uuid4()}/original.pdf",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=2048,
        status=status,
        searchable=searchable,
        current_version=current_version,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(document)
    await session.flush()

    png = _png()
    digest = hashlib.sha256(png).hexdigest()
    key = artifact_key(
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=kb.id,
        document_id=document.id,
        version=figure_version,
        name=f"figures/{digest}.png",
    )
    if store_object:
        await storage.put(key, png, content_type="image/png")
    session.add(
        DocumentFigure(
            document_id=document.id,
            document_version=figure_version,
            page_number=7,
            figure_index=0,
            content_sha256=digest,
            storage_uri=storage.uri_for(key),
            caption="FIGURE 3",
            bbox_x0=10.0,
            bbox_y0=20.0,
            bbox_x1=110.0,
            bbox_y1=140.0,
            width_px=1,
            height_px=1,
            byte_size=len(png),
        )
    )
    await session.flush()
    return document, digest


# --- the success path ---------------------------------------------------------


async def test_the_owner_is_served_the_raster_inline(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.content == _png()
    assert response.headers["content-type"] == "image/png"
    # NFR-SEC-10: inline, never a download — and no `filename=`, which would both suggest one
    # and hand out a name derived from the uploaded file.
    assert response.headers["content-disposition"] == "inline"
    assert "filename" not in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_the_cache_lifetime_is_long_and_private(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    """R-94(5): the URL is content-addressed, so `immutable` is honest.

    `private` is the load-bearing word — the bytes are one user's document and the URL carries
    no principal, so a shared cache holding them would serve them to whoever asked next.
    """
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)

    cache = response.headers["cache-control"]
    assert "private" in cache
    assert "immutable" in cache
    assert "max-age=31536000" in cache


async def test_a_credential_is_required(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    owner, _ = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner)

    assert (await client.get(f"{API}/{document.id}/figures/{digest}")).status_code == 401


# --- who may not be served ----------------------------------------------------


async def test_a_foreign_caller_gets_404(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    owner, _ = await _caller(session, make_token)
    _, stranger_headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{digest}", headers=stranger_headers)

    assert response.status_code == 404
    # Never 403: that would confirm the id names something real (NFR-SEC-02).
    assert response.json()["detail"] == "Figure not found."


async def test_an_administrator_gets_404_on_another_users_figure(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    """**The deliberate departure from every sibling route, and the one to defend.**

    FR-USR-04 widens `GET /documents/{id}`, `DELETE`, `/retry` and `/replace` for an
    administrator, so the natural instinct is to widen this too. NFR-SEC-10 requires "the same
    predicate as FR-RET-04", which has no administrator branch — and the substantive reason is
    that the siblings disclose *management* while this discloses *content*. Widening it would
    make this the first route in Corpus by which an administrator reads another user's
    document.
    """
    owner, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    document, digest = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{digest}", headers=admin_headers)

    assert response.status_code == 404


# --- which documents may not be served ----------------------------------------


async def test_a_deleted_document_serves_nothing(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:  # noqa: ANN001
    """Unlike `GET /documents/{id}`, which returns a tombstone so a polling client sees
    `DELETED`. There is nothing to poll here, and NFR-SEC-10 names deletion explicitly."""
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner, deleted=True)

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 404


async def test_a_document_mid_replace_serves_nothing(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:
    """The state that tells `status` and `searchable` apart, which is why it is its own test.

    R-40(3) keeps the previous version answering while a replacement is built, so a document
    being replaced is `searchable` **and** not `ACTIVE`. `_access_predicates` omits status for
    exactly that reason; NFR-SEC-10 names it, so the figure route has it and its chunks do not.

    The consequence is worth stating rather than discovering: a citation to a document that is
    mid-replace shows no figure for the duration, and FR-CIT-07 says such a citation renders
    exactly as it does today.
    """
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(
        session, storage, owner_id=owner, status=DocumentStatus.PARSING, searchable=True
    )

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 404


async def test_an_unsearchable_document_serves_nothing(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:
    """The other half of the pair — `ACTIVE` and not `searchable`, so neither predicate is free
    to be deleted without a test failing (§8.65(5): a duplicated guard hides its twin)."""
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(
        session, storage, owner_id=owner, status=DocumentStatus.ACTIVE, searchable=False
    )

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 404


async def test_a_figure_of_a_superseded_version_serves_nothing(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:
    """R-36's collect removes these, so this is defence in depth — and it is what makes a row
    that outlived its version unreachable rather than a 404 someone has to explain."""
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(
        session, storage, owner_id=owner, current_version=2, figure_version=1
    )

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 404


async def test_an_unknown_figure_id_is_the_same_404(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:
    owner, headers = await _caller(session, make_token)
    document, _ = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{'a' * 64}", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Figure not found."


@pytest.mark.parametrize(
    "bad",
    ["short", "A" * 64, "g" * 64, "0" * 63, "0" * 65],
)
async def test_a_malformed_id_never_reaches_a_query(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage, bad: str
) -> None:
    """Rejected by the path's own pattern. It discloses nothing: an id of the wrong shape
    cannot name a figure that exists, so there is no existence to confirm."""
    owner, headers = await _caller(session, make_token)
    document, _ = await _figure(session, storage, owner_id=owner)

    response = await client.get(f"{API}/{document.id}/figures/{bad}", headers=headers)

    assert response.status_code == 422


# --- storage failures ---------------------------------------------------------


async def test_a_missing_object_is_404_and_never_500(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage
) -> None:
    """A row without its bytes should not arise — one `put` precedes one transaction — but
    "should not arise" is the wrong assumption for a route to make about object storage, and
    the alternative is a 500 on a page that is merely missing a picture."""
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner, store_object=False)

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 404


async def test_storage_being_down_is_503_rather_than_404(
    client, session: AsyncSession, make_token, storage: LocalFilesystemStorage, monkeypatch
) -> None:
    """ "No such figure" and "the bucket is unreachable" are different facts, and collapsing
    them teaches a client to stop asking for something that exists (R-33's normative 503)."""
    owner, headers = await _caller(session, make_token)
    document, digest = await _figure(session, storage, owner_id=owner)

    async def _down(key: str) -> bytes:
        raise ObjectStorageError("bucket unreachable")

    monkeypatch.setattr(storage, "get", _down)

    assert (
        await client.get(f"{API}/{document.id}/figures/{digest}", headers=headers)
    ).status_code == 503


# --- R-31 is narrowed, not repealed -------------------------------------------


def test_no_route_serves_the_uploaded_file() -> None:
    """NFR-SEC-10 admits **a re-encoded raster** and nothing else.

    Stated over the whole published contract rather than over this handler, because "the
    original is unservable" is a claim about every route there is. Two things are checked: the
    only binary response body anywhere is this route's PNG, and no response schema exposes a
    `storage_uri` a client could dereference. R-31's revisit trigger was discharged by R-32's
    scanner; the file itself staying unservable is what keeps FR-CIT-05 declined.
    """
    spec: dict[str, Any] = json.loads(
        (Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8")
    )

    served: set[str] = set()
    for path, item in spec["paths"].items():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            for status_code, response in operation.get("responses", {}).items():
                for media in response.get("content", {}):
                    if media not in {"application/json", "text/event-stream"}:
                        served.add(f"{path} {status_code} {media}")

    assert served == {"/api/v1/documents/{document_id}/figures/{content_sha256} 200 image/png"}, (
        "a route publishes a non-JSON body other than the FR-ING-09 figure — R-31 keeps the "
        "uploaded file unservable, and NFR-SEC-10 narrows that for a re-encoded raster only"
    )

    # Property **names**, not a JSON dump of the schemas: the models' docstrings become
    # `description` strings, and several of them discuss `storage_uri` precisely to say it is
    # absent — so a substring search over the dump matches the prose explaining the rule and
    # fails against a contract that obeys it.
    fields: set[str] = set()
    for schema in spec.get("components", {}).get("schemas", {}).values():
        fields.update(schema.get("properties", {}))
    assert "storage_uri" not in fields, (
        "a response schema exposes an object-storage URI; R-40(5) keeps the read surface "
        "metadata-only so R-31's revisit trigger stays untripped"
    )


# --- the CSP the edge serves (T-716, FR-CIT-07) -------------------------------


def test_any_content_security_policy_admits_the_figures_blob() -> None:
    """FR-CIT-07's figure is a `blob:` URL, so a CSP that forgets it hides every figure.

    Written by T-716 when there was no policy at all, and **live since T-719 added one**
    (NFR-SEC-11). The failure it guards is silent and delayed: the route is authenticated, so the
    bytes cannot be reached by putting its URL in `src`; the client fetches them and renders an
    object URL. `img-src 'self'` — the obvious thing to write, since everything else here *is*
    same-origin — would leave every citation's figure blank, with no error a user or an operator
    would connect to the header.

    Note that nginx's `add_header` does not inherit across levels, so a server-level policy would
    have to be repeated in the two `location` blocks that set `Cache-Control`.
    """
    # **Every** nginx file, not just `default.conf`. T-719 put the policy in `security.inc`,
    # and a version of this that read one file by name went on passing with a policy live in
    # the next one — which is the exact failure a vacuous guard is: it certifies.
    directives = [
        line.strip()
        for path in sorted((REPO_ROOT / "deployment" / "nginx").glob("*.*"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "content-security-policy" in line.lower() and not line.strip().startswith("#")
    ]
    assert directives, (
        "no Content-Security-Policy found under deployment/nginx/. NFR-SEC-11 requires one; if "
        "it moved, this guard has to follow it or it protects nothing."
    )
    for directive in directives:
        policy = directive.lower()
        source_list = policy.split("img-src", 1)[1] if "img-src" in policy else policy
        assert "blob:" in source_list, (
            f"the CSP at the edge does not admit blob: for images:\n    {directive}\n"
            "FR-CIT-07 renders a figure from an object URL, because the route serving it is "
            "authenticated and cannot be reached from an <img src>. Add `blob:` to img-src."
        )
