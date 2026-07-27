"""Object-storage service tests (T-201, R-19, FR-ING-02/05).

The filesystem backend is exercised in full (it is the dev backend R-19 sanctions and
needs no external service). The S3/MinIO backend is exercised against the developer's
local MinIO when it is reachable and **skips** otherwise, following the conftest
`session` fixture's "skip, don't error" convention.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import PurePath

import pytest
from botocore.exceptions import ClientError

from app.config import MinioSettings, Settings, StorageSettings
from app.services import object_storage as storage_module
from app.services.health import check_object_storage
from app.services.object_storage import (
    InvalidKeyError,
    LocalFilesystemStorage,
    ObjectNotFoundError,
    ObjectTooLargeError,
    S3ObjectStorage,
    artifact_key,
    build_object_storage,
    close_object_storage,
    document_prefix,
    get_object_storage,
    original_key,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")
KB = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOC = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def local_storage(tmp_path) -> LocalFilesystemStorage:  # noqa: ANN001
    return LocalFilesystemStorage(tmp_path)


async def _aiter(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- key layout ---------------------------------------------------------------


def test_original_key_is_tenant_scoped_and_versioned() -> None:
    key = original_key(
        tenant_id=TENANT,
        knowledge_base_id=KB,
        document_id=DOC,
        version=2,
        filename="Quarterly Report.PDF",
    )
    assert key == f"tenants/{TENANT}/kb/{KB}/documents/{DOC}/v2/original.pdf"
    # The version prefix nests inside the document prefix, so FR-ING-05 purges
    # every version with a single prefix delete.
    assert key.startswith(document_prefix(tenant_id=TENANT, knowledge_base_id=KB, document_id=DOC))


def test_original_key_drops_unaccepted_and_hostile_extensions() -> None:
    """Only FR-ERR-03 suffixes survive; the rest of the filename never reaches the key."""
    for filename in ("payload.exe", "../../etc/passwd", "note.txt", "archive.tar.gz"):
        key = original_key(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            document_id=DOC,
            version=1,
            filename=filename,
        )
        assert key.endswith("/v1/original")


def test_artifact_key_lives_under_the_version_prefix() -> None:
    key = artifact_key(
        tenant_id=TENANT, knowledge_base_id=KB, document_id=DOC, version=1, name="text.json"
    )
    assert key == f"tenants/{TENANT}/kb/{KB}/documents/{DOC}/v1/artifacts/text.json"


@pytest.mark.parametrize("bad", ["", "/absolute/key", "a\\b", "../escape", "a/../../b", "C:/x"])
def test_invalid_keys_are_rejected(local_storage: LocalFilesystemStorage, bad: str) -> None:
    with pytest.raises(InvalidKeyError):
        local_storage.uri_for(bad)


async def test_traversing_key_cannot_escape_the_root(
    local_storage: LocalFilesystemStorage,
) -> None:
    with pytest.raises(InvalidKeyError):
        await local_storage.put("../outside.pdf", b"nope")
    outside = PurePath(local_storage.root).parent / "outside.pdf"
    assert not await asyncio.to_thread(os.path.exists, outside)


# --- local filesystem backend -------------------------------------------------


async def test_put_get_round_trip(local_storage: LocalFilesystemStorage) -> None:
    stored = await local_storage.put(
        "docs/a/original.pdf", b"hello", content_type="application/pdf"
    )

    assert stored.key == "docs/a/original.pdf"
    assert stored.size == 5
    assert stored.content_type == "application/pdf"
    assert stored.uri == "file:///docs/a/original.pdf"
    assert local_storage.key_for_uri(stored.uri) == stored.key
    assert await local_storage.get("docs/a/original.pdf") == b"hello"
    assert await local_storage.exists("docs/a/original.pdf") is True


async def test_put_accepts_an_async_iterator(local_storage: LocalFilesystemStorage) -> None:
    stored = await local_storage.put("k", _aiter(b"ab", b"cd", b"e"))

    assert stored.size == 5
    assert await local_storage.get("k") == b"abcde"


async def test_put_overwrites(local_storage: LocalFilesystemStorage) -> None:
    await local_storage.put("k", b"first")
    await local_storage.put("k", b"second")
    assert await local_storage.get("k") == b"second"


async def test_max_bytes_aborts_before_anything_is_written(
    local_storage: LocalFilesystemStorage,
) -> None:
    """R-31: the size limit is enforced *before* the object reaches storage."""
    with pytest.raises(ObjectTooLargeError):
        await local_storage.put("big", _aiter(b"x" * 4, b"y" * 4), max_bytes=5)
    assert await local_storage.exists("big") is False

    with pytest.raises(ObjectTooLargeError):
        await local_storage.put("big", b"x" * 6, max_bytes=5)
    assert await local_storage.exists("big") is False


async def test_stream_yields_the_object_in_order(local_storage: LocalFilesystemStorage) -> None:
    await local_storage.put("k", b"abcdefg")

    chunks = [chunk async for chunk in local_storage.stream("k", chunk_size=3)]

    assert chunks == [b"abc", b"def", b"g"]


async def test_missing_key_raises_not_found(local_storage: LocalFilesystemStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        await local_storage.get("nope")
    with pytest.raises(ObjectNotFoundError):
        [chunk async for chunk in local_storage.stream("nope")]
    assert await local_storage.exists("nope") is False


async def test_delete_is_idempotent(local_storage: LocalFilesystemStorage) -> None:
    """FR-ING-04 retries re-run the delete; a missing key must not fail the job."""
    await local_storage.put("k", b"x")
    await local_storage.delete("k")
    await local_storage.delete("k")
    assert await local_storage.exists("k") is False


async def test_delete_prefix_removes_every_version(
    local_storage: LocalFilesystemStorage,
) -> None:
    prefix = document_prefix(tenant_id=TENANT, knowledge_base_id=KB, document_id=DOC)
    for version in (1, 2):
        await local_storage.put(
            original_key(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id=DOC,
                version=version,
                filename="f.pdf",
            ),
            b"x",
        )
    await local_storage.put(
        artifact_key(
            tenant_id=TENANT, knowledge_base_id=KB, document_id=DOC, version=1, name="text.json"
        ),
        b"{}",
    )
    # A sibling document must survive the purge.
    other = uuid.uuid4()
    await local_storage.put(
        original_key(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            document_id=other,
            version=1,
            filename="f.pdf",
        ),
        b"keep",
    )

    removed = await local_storage.delete_prefix(prefix)

    assert removed == 3
    assert await local_storage.delete_prefix(prefix) == 0  # idempotent
    assert await local_storage.exists(
        original_key(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            document_id=other,
            version=1,
            filename="f.pdf",
        )
    )


async def test_local_health_creates_and_accepts_a_writable_root(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "objects"
    await LocalFilesystemStorage(root).check_health()
    assert root.is_dir()


# --- factory / DI -------------------------------------------------------------


def test_backend_selection_follows_settings(tmp_path) -> None:  # noqa: ANN001
    local = Settings(storage=StorageSettings(backend="local", local_root=str(tmp_path)))
    s3 = Settings(storage=StorageSettings(backend="s3"), minio=MinioSettings(bucket="corpus"))

    assert isinstance(build_object_storage(local), LocalFilesystemStorage)
    assert isinstance(build_object_storage(s3), S3ObjectStorage)


async def test_get_object_storage_is_cached_until_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.setattr(
        storage_module,
        "build_object_storage",
        lambda settings=None: LocalFilesystemStorage(tmp_path),
    )

    first = get_object_storage()
    assert get_object_storage() is first

    await close_object_storage()
    assert get_object_storage() is not first
    await close_object_storage()


async def test_readiness_probe_uses_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """T-106's object-storage probe now delegates to the T-201 backend."""
    monkeypatch.setattr(storage_module, "_storage", LocalFilesystemStorage(tmp_path))
    ok = await check_object_storage()
    assert ok.status == "ok"

    class _Broken(LocalFilesystemStorage):
        async def check_health(self) -> None:
            raise RuntimeError("bucket missing")

    monkeypatch.setattr(storage_module, "_storage", _Broken(tmp_path))
    down = await check_object_storage()
    assert down.status == "error"
    assert "bucket missing" in (down.error or "")


# --- S3 backend, offline (fake client) ---------------------------------------
#
# These cover the logic that is ours rather than botocore's — URI construction, error
# translation, and the paginate/batch loop behind `delete_prefix` — so the S3 backend
# has coverage even where MinIO is not running (the live tests below then confirm the
# same behaviour end to end).


def _client_error(code: str, operation: str = "GetObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeS3Client:
    """Minimal stand-in for the aiobotocore S3 client."""

    def __init__(self, *, pages: list[list[str]] | None = None) -> None:
        self.pages = pages or []
        self.calls: list[tuple[str, dict]] = []
        self.deleted_keys: list[str] = []
        self.head_bucket_error: ClientError | None = None
        self.get_error: ClientError | None = None
        self.head_object_error: ClientError | None = None
        self.delete_error: ClientError | None = None

    async def put_object(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("put_object", kwargs))
        return {"ETag": '"abc123"'}

    async def get_object(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("get_object", kwargs))
        if self.get_error:
            raise self.get_error
        return {"Body": _FakeBody(b"payload")}

    async def head_object(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("head_object", kwargs))
        if self.head_object_error:
            raise self.head_object_error
        return {}

    async def head_bucket(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("head_bucket", kwargs))
        if self.head_bucket_error:
            raise self.head_bucket_error
        return {}

    async def create_bucket(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("create_bucket", kwargs))
        return {}

    async def delete_object(self, **kwargs):  # noqa: ANN003, ANN202
        self.calls.append(("delete_object", kwargs))
        if self.delete_error:
            raise self.delete_error
        self.deleted_keys.append(kwargs["Key"])
        return {}

    def get_paginator(self, name: str):  # noqa: ANN202
        assert name == "list_objects_v2"
        return _FakePaginator(self.pages)


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def read(self) -> bytes:
        return self.payload

    async def iter_chunks(self, chunk_size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(self.payload), chunk_size):
            yield self.payload[start : start + chunk_size]


class _FakePaginator:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages

    async def paginate(self, **_kwargs: object) -> AsyncIterator[dict]:
        for page in self.pages:
            yield {"Contents": [{"Key": key} for key in page]}


def _fake_s3(**kwargs) -> tuple[S3ObjectStorage, _FakeS3Client]:  # noqa: ANN003
    store = S3ObjectStorage(Settings(minio=MinioSettings(bucket="corpus")))
    client = _FakeS3Client(**kwargs)
    store._client = client  # noqa: SLF001 — injecting the client is the point
    store._bucket_ready = True  # noqa: SLF001
    return store, client


async def test_s3_put_builds_the_uri_and_forwards_content_type() -> None:
    store, client = _fake_s3()

    stored = await store.put("a/b.pdf", b"hello", content_type="application/pdf")

    assert stored.uri == "s3://corpus/a/b.pdf"
    assert stored.size == 5
    assert stored.etag == "abc123"  # quotes stripped
    _, kwargs = client.calls[-1]
    assert kwargs["Bucket"] == "corpus"
    assert kwargs["Key"] == "a/b.pdf"
    assert kwargs["ContentType"] == "application/pdf"


async def test_s3_translates_missing_key_errors() -> None:
    store, client = _fake_s3()
    client.get_error = _client_error("NoSuchKey")

    with pytest.raises(ObjectNotFoundError):
        await store.get("gone.pdf")

    client.head_object_error = _client_error("404", "HeadObject")
    assert await store.exists("gone.pdf") is False

    # A non-404 failure must surface, not be mistaken for "missing".
    client.head_object_error = _client_error("AccessDenied", "HeadObject")
    with pytest.raises(ClientError):
        await store.exists("gone.pdf")


async def test_s3_delete_prefix_paginates_across_pages() -> None:
    """A purge must cover every listing page, not just the first."""
    page_one = [f"k{i}" for i in range(120)]
    page_two = [f"k{i}" for i in range(120, 130)]
    store, client = _fake_s3(pages=[page_one, page_two])

    removed = await store.delete_prefix("tenants/x/")

    assert removed == 130
    assert set(client.deleted_keys) == set(page_one + page_two)


async def test_s3_delete_prefix_propagates_failures() -> None:
    """A refused delete must fail the purge — FR-ING-05 may not report a false DELETED."""
    store, client = _fake_s3(pages=[["k1"]])
    client.delete_error = _client_error("AccessDenied", "DeleteObject")

    with pytest.raises(ClientError):
        await store.delete_prefix("tenants/x/")


async def test_s3_ensure_bucket_creates_a_missing_bucket() -> None:
    store, client = _fake_s3()
    store._bucket_ready = False  # noqa: SLF001
    client.head_bucket_error = _client_error("404", "HeadBucket")

    await store.ensure_bucket()

    assert any(call == "create_bucket" for call, _ in client.calls)
    # Second call short-circuits — no repeated HeadBucket per upload.
    client.calls.clear()
    await store.ensure_bucket()
    assert client.calls == []


# --- S3 / MinIO backend (live, skipped when MinIO is unreachable) -------------


@pytest.fixture
async def s3_storage() -> AsyncIterator[S3ObjectStorage]:
    """Live MinIO backed by the developer's local instance; skip when unavailable."""
    store = S3ObjectStorage()
    try:
        await store.ensure_bucket()
    except Exception as exc:  # noqa: BLE001 — any failure means "no MinIO", so skip.
        await store.aclose()
        pytest.skip(f"MinIO not reachable for object-storage tests: {exc}")
    try:
        yield store
    finally:
        await store.aclose()


async def test_s3_round_trip_and_purge(s3_storage: S3ObjectStorage) -> None:
    doc = uuid.uuid4()
    prefix = document_prefix(tenant_id=TENANT, knowledge_base_id=KB, document_id=doc)
    key = original_key(
        tenant_id=TENANT, knowledge_base_id=KB, document_id=doc, version=1, filename="f.pdf"
    )
    try:
        stored = await s3_storage.put(key, b"%PDF-1.7 body", content_type="application/pdf")

        assert stored.uri == f"s3://{s3_storage.bucket}/{key}"
        assert s3_storage.key_for_uri(stored.uri) == key
        assert stored.size == 13
        assert stored.etag
        assert await s3_storage.get(key) == b"%PDF-1.7 body"
        assert await s3_storage.exists(key) is True
        assert b"".join([c async for c in s3_storage.stream(key, chunk_size=4)]) == b"%PDF-1.7 body"

        await s3_storage.put(
            artifact_key(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id=doc,
                version=1,
                name="text.json",
            ),
            b"{}",
        )
        assert await s3_storage.delete_prefix(prefix) == 2
        assert await s3_storage.exists(key) is False
    finally:
        await s3_storage.delete_prefix(prefix)


async def test_s3_missing_key_and_idempotent_delete(s3_storage: S3ObjectStorage) -> None:
    key = f"tests/{uuid.uuid4()}/missing.pdf"
    with pytest.raises(ObjectNotFoundError):
        await s3_storage.get(key)
    await s3_storage.delete(key)  # no error
    assert await s3_storage.exists(key) is False


async def test_s3_foreign_uri_is_rejected(s3_storage: S3ObjectStorage) -> None:
    with pytest.raises(InvalidKeyError):
        s3_storage.key_for_uri("s3://someone-elses-bucket/k")
    with pytest.raises(InvalidKeyError):
        s3_storage.key_for_uri("file:///k")


async def test_s3_health_check_passes(s3_storage: S3ObjectStorage) -> None:
    await s3_storage.check_health()
