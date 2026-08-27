"""FR-ING-09 figure persistence, end to end — the lifetime parity R-94(5) asks to be asserted.

**Why these live in `scenarios/` rather than beside the extraction unit tests.** The claim is
about what happens *between* three components: the worker writes rows, `_purge_superseded_versions`
deletes objects, and the delete task removes both. Every half of that is individually plausible,
and the failure R-94(5) names — "a figure surviving its version is a dangling URL, and one purged
early is a broken image on a live answer" — is only visible where the halves meet.

**The object half is asserted although no T-714 code implements it**, and that is deliberate:
rasters live under the existing version-scoped prefix, so `delete_prefix` already removes them
and always did. An assertion over code nobody wrote is exactly right here, because the property
holds by a *layout* decision that a later refactor of the key builder could silently undo.

Figures are off by default (R-94(7)), so every test that wants them turns them on through the
worker's own injected settings — which is also what pins the switch to the one place the worker
is configured.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ParserSettings, ScannerSettings, Settings, WorkerSettings
from app.db.models.document import Document
from app.db.models.document_figure import DocumentFigure
from app.services.object_storage import LocalFilesystemStorage
from tests.scenarios.conftest import (
    DrainingQueue,
    make_caller,
    reload,
    upload_files,
)
from tests.test_figures import make_page

pytestmark = pytest.mark.usefixtures("patch_jwks")


def figure_pdf(nonce: str = "") -> bytes:
    """A page carrying prose, a vector plot and its caption.

    The scenario `pdf()` builder writes text and nothing else, so it produces no figures at
    all — which would make every assertion below pass vacuously. `nonce` exists because
    FR-KBM-08 dedups on checksum: two documents in one test have to differ in their bytes.
    """
    with make_page() as document:
        if nonce:
            document[0].insert_text((72, 700), nonce)
        return document.tobytes()


def figures_on(**parser: object) -> Settings:
    """The worker context's settings, with extraction armed.

    Built here rather than by flipping `get_settings()`: `_store_figures` takes its limits from
    `deps.settings.parser`, and a test that patched the global cache would pass against a worker
    that ignored its own configuration.
    """
    return Settings(
        worker=WorkerSettings(max_tries=5, retry_base_seconds=0.01),
        scanner=ScannerSettings(backend="structural"),
        parser=ParserSettings(figures_enabled=True, ocr_enabled=False, **parser),
    )


def figures_off(**parser: object) -> Settings:
    """The same context with extraction disarmed, stated rather than inherited.

    `queue.drain()` with no override builds its settings from the environment, so a developer
    running the manual test round with `PARSER_FIGURES_ENABLED=true` in `backend/.env` turned
    every "with the feature off" assertion here into its opposite -- and the tests still read as
    if they had pinned it, because one of them says so in a comment. The shipped default being
    off is a claim about `app/config.py`, not about this machine; it is asserted where it can be
    read off the field (`test_figures.py`).
    """
    return Settings(
        worker=WorkerSettings(max_tries=5, retry_base_seconds=0.01),
        scanner=ScannerSettings(backend="structural"),
        parser=ParserSettings(figures_enabled=False, ocr_enabled=False, **parser),
    )


async def figures_of(session: AsyncSession, document_id: object) -> list[DocumentFigure]:
    stmt = (
        select(DocumentFigure)
        .where(DocumentFigure.document_id == document_id)
        .order_by(
            DocumentFigure.document_version, DocumentFigure.page_number, DocumentFigure.figure_index
        )
    )
    return list((await session.scalars(stmt)).all())


async def stored(storage: LocalFilesystemStorage, figure: DocumentFigure) -> bool:
    return await storage.exists(storage.key_for_uri(figure.storage_uri))


# --- the write ----------------------------------------------------------------


async def test_an_ingested_pdf_records_its_figures_against_the_version(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    storage: LocalFilesystemStorage,
) -> None:
    caller = await make_caller(session, make_token)

    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    assert created.status_code == 202, created.text
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_on())

    rows = await figures_of(session, document_id)
    assert rows, "the fixture draws a figure; recording none means the pass never ran"
    assert {row.document_version for row in rows} == {1}
    assert all(row.page_number == 1 for row in rows)
    assert all(row.byte_size > 0 and row.width_px > 0 and row.height_px > 0 for row in rows)
    assert [await stored(storage, row) for row in rows] == [True] * len(rows), (
        "a row whose object is missing is a 404 on a citation that looks fine"
    )

    # The document itself is untouched by any of this — R-94(4): presentation data, never a chunk.
    document = await reload(session, Document, document_id)
    assert document.status.value == "ACTIVE"
    assert document.searchable is True


async def test_the_object_lives_under_the_version_prefix(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
) -> None:
    """The whole of R-94(5)'s object-lifetime argument, stated as a key shape.

    Nothing in T-714 purges a figure. It does not have to — `delete_prefix` on `v{n}/` and on
    the document prefix already reach anything under here. That is true only while the key sits
    inside `document_version_prefix`, so this is the assertion that keeps it true.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_on())

    row = (await figures_of(session, document_id))[0]

    assert f"/documents/{document_id}/v1/" in row.storage_uri
    assert row.storage_uri.endswith(f"/artifacts/figures/{row.content_sha256}.png")


# --- lifetime parity: replace -------------------------------------------------


async def test_a_replace_collects_the_superseded_version_rows_and_objects(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    storage: LocalFilesystemStorage,
) -> None:
    """R-94(5)'s first half, and the reason this file exists.

    Both directions are asserted, because each failure is silent on its own: a v1 row that
    survives points at bytes the purge has deleted (a dangling URL FR-CIT-07 would resolve to a
    404), and a v2 object that was never written is a broken image on an answer that works.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf("first")), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_on())

    v1 = await figures_of(session, document_id)
    assert v1, "nothing to supersede"
    v1_keys = [storage.key_for_uri(row.storage_uri) for row in v1]

    replaced = await client.post(
        f"/api/v1/documents/{document_id}/replace",
        files=upload_files(figure_pdf("second")),
        headers=caller.headers,
    )
    assert replaced.status_code == 202, replaced.text
    await queue.drain(settings_override=figures_on())

    rows = await figures_of(session, document_id)
    assert rows, "the replace produced no figures at all"
    assert {row.document_version for row in rows} == {2}, (
        "a superseded version's figures must be collected with its chunks (R-94(5)) — a row "
        "outliving its version is a URL that resolves to nothing"
    )
    assert [await stored(storage, row) for row in rows] == [True] * len(rows)
    for key in v1_keys:
        assert not await storage.exists(key), (
            "v1's rasters survived the purge; the key has left the version prefix"
        )


async def test_a_replace_with_figures_turned_off_leaves_no_stale_rows(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
) -> None:
    """Turning the feature off must not be a way to create dangling rows.

    This is why the collect runs unconditionally rather than only when something was extracted.
    Skipping it when the pass yields nothing would leave v1's rows behind — pointing at objects
    the purge has just deleted — and the operator's only action was to switch a feature off.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf("armed")), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_on())
    assert await figures_of(session, document_id)

    replaced = await client.post(
        f"/api/v1/documents/{document_id}/replace",
        files=upload_files(figure_pdf("disarmed")),
        headers=caller.headers,
    )
    assert replaced.status_code == 202, replaced.text
    await queue.drain(settings_override=figures_off())

    assert await figures_of(session, document_id) == []


# --- lifetime parity: delete --------------------------------------------------


async def test_deleting_a_document_removes_its_figures_and_their_objects(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    storage: LocalFilesystemStorage,
) -> None:
    """R-94(5)'s second half, under R-39: the rows go in the terminal transaction, the objects
    with the document prefix purged before it."""
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_on())

    keys = [storage.key_for_uri(row.storage_uri) for row in await figures_of(session, document_id)]
    assert keys

    removed = await client.delete(f"/api/v1/documents/{document_id}", headers=caller.headers)
    assert removed.status_code == 202, removed.text
    await queue.drain(settings_override=figures_on())

    assert await figures_of(session, document_id) == []
    for key in keys:
        assert not await storage.exists(key)


# --- failing open -------------------------------------------------------------


async def test_a_storage_failure_costs_the_figures_and_never_the_document(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    storage: LocalFilesystemStorage,
    monkeypatch,
) -> None:
    """FR-ING-09 fails open, and by this point the document is already `ACTIVE` and answering.

    The put is broken *after* the original upload has been stored, so the failure lands on the
    figure write alone — patching it earlier would fail the ingestion for an unrelated reason
    and prove nothing about this path.
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    document_id = created.json()["document_id"]

    real_put = storage.put

    async def _fail_on_figures(key: str, data, **kwargs):
        if "/artifacts/figures/" in key:
            raise RuntimeError("object storage went away")
        return await real_put(key, data, **kwargs)

    monkeypatch.setattr(storage, "put", _fail_on_figures)
    ran = await queue.drain(settings_override=figures_on())

    assert [outcome.retried for outcome in ran] == [False]
    assert [outcome.error for outcome in ran] == [None]
    document = await reload(session, Document, document_id)
    assert document.status.value == "ACTIVE", "a picture must never fail an ingested document"
    assert document.searchable is True
    assert await figures_of(session, document_id) == []


async def test_a_redelivered_job_rewrites_the_same_figures_rather_than_doubling_them(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
) -> None:
    """FR-ING-04's redelivery, against a unique constraint that would otherwise raise.

    The delete-then-insert in `replace_for_version` is unconditional for exactly this: a retry
    is the normal case here, not the exception, and an integrity error would be swallowed by
    the fail-open handler and read as "this document has no figures".
    """
    caller = await make_caller(session, make_token)
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    ran = await queue.drain(settings_override=figures_on())

    first = await figures_of(session, document_id)
    assert first

    await queue.redeliver(ran[0].job, settings_override=figures_on())
    session.expire_all()
    again = await figures_of(session, document_id)

    assert len(again) == len(first)
    assert [row.content_sha256 for row in again] == [row.content_sha256 for row in first]


# --- off is off ---------------------------------------------------------------


async def test_the_default_configuration_stores_no_figures_at_all(
    client,
    session: AsyncSession,
    make_token: Callable[..., str],
    queue: DrainingQueue,
    storage: LocalFilesystemStorage,
) -> None:
    """R-94(7). Asserted on the storage too: no rows is also what a working feature with a
    broken write looks like."""
    caller = await make_caller(session, make_token)
    puts: list[str] = []
    real_put = storage.put

    async def _record(key: str, data, **kwargs):
        puts.append(key)
        return await real_put(key, data, **kwargs)

    storage.put = _record  # type: ignore[method-assign]
    created = await client.post(
        "/api/v1/documents", files=upload_files(figure_pdf()), headers=caller.headers
    )
    document_id = created.json()["document_id"]
    await queue.drain(settings_override=figures_off())

    assert await figures_of(session, document_id) == []
    assert not [key for key in puts if "/artifacts/" in key]
    document = await reload(session, Document, document_id)
    assert document.status.value == "ACTIVE"
