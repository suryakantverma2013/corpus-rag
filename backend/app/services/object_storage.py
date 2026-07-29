"""Object storage for original uploads and derived artifacts (T-201, R-19).

R-19 adopts MinIO/S3 as the baseline store for original files and derived artifacts
(FR-ING-02 writes them, FR-ING-05 purges them) and explicitly allows a filesystem
backend for local development. Both are implemented here behind one protocol:

* :class:`S3ObjectStorage` — MinIO **and** AWS S3 over ``aioboto3`` (native async).
* :class:`LocalFilesystemStorage` — a directory tree; dev/CI only.

Callers work in **keys**, never paths or buckets. :func:`original_key` /
:func:`artifact_key` build the canonical, tenant-scoped layout::

    tenants/{tenant_id}/kb/{knowledge_base_id}/documents/{document_id}/v{version}/original.pdf
    tenants/{tenant_id}/kb/{knowledge_base_id}/documents/{document_id}/v{version}/artifacts/{name}

The document row stores the URI (`documents.storage_uri`) that :meth:`ObjectStorage.uri_for`
returns; :meth:`ObjectStorage.key_for_uri` maps it back for reads and deletes. Version
scoping means FR-ING-05 deletion and T-209 replace both reduce to a prefix delete.

**No read-back surface is exposed on purpose.** There is deliberately no presigned-URL
or download helper here: R-31 (§8.12) defers malware scanning to a no-op seam precisely
because Corpus never re-serves an uploaded file, and adding a download/export/preview
path is the ruling's revisit trigger. Ingestion reads objects server-side via
:meth:`ObjectStorage.stream`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import PurePosixPath
from typing import Annotated, Protocol, runtime_checkable

import structlog
from fastapi import Depends
from pydantic import BaseModel

from app.config import Settings, get_settings

log = structlog.get_logger(__name__)

# Upload formats accepted by FR-ERR-03. Used only to normalise the stored object's
# suffix — the authoritative check is T-202's magic-byte sniff (R-31), not this list.
ALLOWED_SUFFIXES = frozenset({".pdf", ".docx", ".csv", ".md"})

_DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming reads
_S3_DELETE_CONCURRENCY = 50  # in-flight single-object deletes per prefix purge


# --- errors ------------------------------------------------------------------


class ObjectStorageError(Exception):
    """Base class for object-storage failures."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested key does not exist."""


class ObjectTooLargeError(ObjectStorageError):
    """A streamed body exceeded the caller's ``max_bytes`` ceiling.

    Raised *before* the object is written, so the FR-ERR-01 50 MB limit can be
    enforced pre-storage as R-31 requires.
    """


class InvalidKeyError(ObjectStorageError):
    """The key is empty, absolute, or escapes the storage root."""


# --- value objects -----------------------------------------------------------


class StoredObject(BaseModel):
    """Result of a successful write."""

    key: str
    uri: str
    size: int
    content_type: str | None = None
    etag: str | None = None


# --- key layout --------------------------------------------------------------


def _validate_key(key: str) -> str:
    """Reject empty, absolute, or traversing keys.

    S3 would happily store ``../`` as a literal key name; the local backend would
    follow it out of the storage root. One rule for both keeps the two backends
    behaviourally identical.
    """
    if not key or key != key.strip():
        raise InvalidKeyError("storage key must be a non-empty, unpadded string")
    if key.startswith("/") or "\\" in key or (len(key) > 1 and key[1] == ":"):
        raise InvalidKeyError(f"storage key must be a relative POSIX path: {key!r}")
    if any(part in {"..", "."} for part in PurePosixPath(key).parts):
        raise InvalidKeyError(f"storage key must not contain '.' or '..' segments: {key!r}")
    return key


def _safe_suffix(filename: str) -> str:
    """Return the lower-cased suffix if it is an accepted upload format, else ``""``.

    Keeps attacker-controlled filenames out of the key: only the extension survives,
    and only from the FR-ERR-03 allow-list.
    """
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return suffix if suffix in ALLOWED_SUFFIXES else ""


def document_prefix(
    *, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID, document_id: uuid.UUID
) -> str:
    """Prefix covering every version and artifact of one document (FR-ING-05 purge)."""
    return f"tenants/{tenant_id}/kb/{knowledge_base_id}/documents/{document_id}/"


def document_version_prefix(
    *, tenant_id: uuid.UUID, knowledge_base_id: uuid.UUID, document_id: uuid.UUID, version: int
) -> str:
    """Prefix covering one version of a document (T-209 replace)."""
    prefix = document_prefix(
        tenant_id=tenant_id, knowledge_base_id=knowledge_base_id, document_id=document_id
    )
    return f"{prefix}v{version}/"


def original_key(
    *,
    tenant_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    filename: str,
) -> str:
    """Key of the original uploaded file for a document version (FR-ING-02)."""
    prefix = document_version_prefix(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        version=version,
    )
    return f"{prefix}original{_safe_suffix(filename)}"


def artifact_key(
    *,
    tenant_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    version: int,
    name: str,
) -> str:
    """Key of a derived artifact (extracted text, page images, …) for a version."""
    prefix = document_version_prefix(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        version=version,
    )
    return _validate_key(f"{prefix}artifacts/{name}")


# --- protocol ----------------------------------------------------------------


@runtime_checkable
class ObjectStorage(Protocol):
    """The seam every caller depends on — S3 and filesystem both satisfy it."""

    async def put(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredObject:
        """Write ``data`` at ``key``, overwriting. Raises :class:`ObjectTooLargeError`."""
        ...

    async def get(self, key: str) -> bytes:
        """Read the whole object. Raises :class:`ObjectNotFoundError`."""
        ...

    def stream(self, key: str, *, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Yield the object in chunks (worker-side reads of up-to-50 MB originals)."""
        ...

    async def delete(self, key: str) -> None:
        """Delete one key. Idempotent — a missing key is not an error (FR-ING-04 retries)."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under ``prefix``; return the count removed (FR-ING-05)."""
        ...

    async def exists(self, key: str) -> bool: ...

    def uri_for(self, key: str) -> str:
        """Canonical URI stored in ``documents.storage_uri``."""
        ...

    def key_for_uri(self, uri: str) -> str:
        """Inverse of :meth:`uri_for`. Raises :class:`InvalidKeyError` on a foreign URI."""
        ...

    async def check_health(self) -> None:
        """Raise if the backend is unusable (readiness probe, NFR-REL-02)."""
        ...

    async def aclose(self) -> None:
        """Release any pooled connections."""
        ...


async def _materialize(data: bytes | AsyncIterator[bytes], max_bytes: int | None) -> bytes:
    """Collect ``data`` into bytes, aborting as soon as ``max_bytes`` is exceeded.

    Single-shot writes are fine at this scale: FR-ERR-01 caps uploads at 50 MB and
    FR-KBM-08's SHA-256 dedup already requires the caller to see the whole body. If
    that ceiling ever rises, switch the S3 backend to multipart here.
    """
    if isinstance(data, bytes | bytearray):
        payload = bytes(data)
        if max_bytes is not None and len(payload) > max_bytes:
            raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
        return payload

    buffer = bytearray()
    async for chunk in data:
        buffer.extend(chunk)
        if max_bytes is not None and len(buffer) > max_bytes:
            # Abort mid-stream: nothing has been written to storage yet (R-31).
            raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
    return bytes(buffer)


# --- local filesystem backend ------------------------------------------------


class LocalFilesystemStorage:
    """Filesystem backend for local development (R-19: "Local dev may use the filesystem").

    Keys map to paths under ``root``; every key is validated so it cannot escape it.
    Blocking file I/O runs in a worker thread so the event loop is never stalled.
    URIs are ``file:///<key>`` — root-relative, so moving the root does not invalidate
    rows already written to ``documents.storage_uri``.
    """

    scheme = "file"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = os.path.abspath(os.fspath(root))

    # -- paths
    def _path(self, key: str) -> str:
        _validate_key(key)
        path = os.path.abspath(os.path.join(self.root, key))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise InvalidKeyError(f"storage key escapes the storage root: {key!r}")
        return path

    def uri_for(self, key: str) -> str:
        return f"file:///{_validate_key(key)}"

    def key_for_uri(self, uri: str) -> str:
        if not uri.startswith("file:///"):
            raise InvalidKeyError(f"not a local-storage URI: {uri!r}")
        return _validate_key(uri.removeprefix("file:///"))

    # -- operations
    async def put(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredObject:
        payload = await _materialize(data, max_bytes)
        path = self._path(key)
        await asyncio.to_thread(self._write, path, payload)
        return StoredObject(
            key=key,
            uri=self.uri_for(key),
            size=len(payload),
            content_type=content_type,
        )

    @staticmethod
    def _write(path: str, payload: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-replace so a crash mid-write never leaves a truncated object
        # that ingestion would happily parse.
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                os.unlink(tmp)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(_read_file, path)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    async def stream(
        self, key: str, *, chunk_size: int = _DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            handle = await asyncio.to_thread(open, path, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(_unlink_quiet, path)

    async def delete_prefix(self, prefix: str) -> int:
        _validate_key(prefix)
        return await asyncio.to_thread(self._delete_prefix, prefix)

    def _delete_prefix(self, prefix: str) -> int:
        directory = self._path(prefix.rstrip("/"))
        if not os.path.isdir(directory):
            return 0
        removed = sum(len(files) for _, _, files in os.walk(directory))
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(os.path.isfile, self._path(key))

    async def check_health(self) -> None:
        def _probe() -> None:
            os.makedirs(self.root, exist_ok=True)
            if not os.access(self.root, os.W_OK):
                raise ObjectStorageError(f"storage root is not writable: {self.root}")

        await asyncio.to_thread(_probe)

    async def aclose(self) -> None:  # nothing pooled
        return None


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _unlink_quiet(path: str) -> None:
    with suppress(FileNotFoundError, IsADirectoryError, PermissionError):
        os.unlink(path)


# --- S3 / MinIO backend ------------------------------------------------------

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


def _translate_s3_error(exc: Exception, key: str | None) -> ObjectStorageError:
    if key is not None and _error_code(exc) in _MISSING_CODES:
        return ObjectNotFoundError(key)
    return ObjectStorageError(f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def _s3_errors(key: str | None = None) -> AsyncIterator[None]:
    """Turn every botocore failure into an :class:`ObjectStorageError`.

    The seam's whole point is that callers above it never import botocore, and they act on
    that: the upload and replace routes map `ObjectStorageError` to the normative `503`, and
    the ingestion worker's `_classify` maps it to a **retryable** `OBJECT_STORAGE_UNAVAILABLE`.
    Anything that escapes as a raw botocore exception therefore lands in the wrong bucket at
    every layer at once.

    **`ClientError` alone is not enough**, which is how this was missed: a bucket that
    answers with an error code raises `ClientError`, but a daemon that is simply *down*
    raises `EndpointConnectionError` — a `BotoCoreError`, a sibling class, not a subclass.
    So the only failure mode the translation covered was the one where the service is
    reachable. Verified live (T-213): with MinIO stopped, `POST /api/v1/documents` propagated
    `EndpointConnectionError` out of the route and answered `500` instead of `503`, and the
    purge dead-lettered as the generic `DELETE_FAILED` rather than `OBJECT_STORAGE_UNAVAILABLE`.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        raise _translate_s3_error(exc, key) from exc


class S3ObjectStorage:
    """MinIO / AWS S3 backend over ``aioboto3`` (R-19).

    One client is created lazily and reused for the process lifetime — botocore client
    construction is expensive, and the client is bound to the event loop that built it,
    so it is torn down by :meth:`aclose` from the app lifespan. URIs are ``s3://bucket/key``.
    """

    scheme = "s3"

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        minio = settings.minio
        self.bucket = minio.bucket
        self._endpoint_url = f"{'https' if minio.secure else 'http'}://{minio.endpoint}"
        self._region = minio.region
        self._access_key = minio.access_key
        self._secret_key = minio.secret_key
        self._timeout = settings.storage.timeout_seconds
        self._auto_create_bucket = settings.storage.auto_create_bucket
        self._client = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self._bucket_ready = False

    # -- client lifecycle
    async def _get_client(self):  # noqa: ANN202 — aiobotocore's client type is dynamic
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                import aioboto3
                from botocore.config import Config

                stack = AsyncExitStack()
                session = aioboto3.Session()
                self._client = await stack.enter_async_context(
                    session.client(
                        "s3",
                        endpoint_url=self._endpoint_url,
                        region_name=self._region,
                        aws_access_key_id=self._access_key,
                        aws_secret_access_key=self._secret_key,
                        config=Config(
                            signature_version="s3v4",
                            connect_timeout=self._timeout,
                            read_timeout=self._timeout,
                            retries={"max_attempts": 3, "mode": "standard"},
                            # MinIO serves path-style buckets; AWS accepts it too.
                            s3={"addressing_style": "path"},
                        ),
                    )
                )
                self._stack = stack
        return self._client

    async def ensure_bucket(self) -> None:
        """Create the configured bucket if it is missing (dev/CI convenience)."""
        if self._bucket_ready:
            return
        from botocore.exceptions import ClientError

        client = await self._get_client()
        async with _s3_errors():
            try:
                await client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if _error_code(exc) not in _MISSING_CODES or not self._auto_create_bucket:
                    raise
                try:
                    await client.create_bucket(Bucket=self.bucket)
                except ClientError as create_exc:
                    # Lost a race with another worker — that is a success for our purposes.
                    if _error_code(create_exc) not in {
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    }:
                        raise
                log.info("object_storage.bucket_created", bucket=self.bucket)
        self._bucket_ready = True

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None
        self._bucket_ready = False

    # -- URIs
    def uri_for(self, key: str) -> str:
        return f"s3://{self.bucket}/{_validate_key(key)}"

    def key_for_uri(self, uri: str) -> str:
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            raise InvalidKeyError(f"URI does not belong to bucket {self.bucket!r}: {uri!r}")
        return _validate_key(uri.removeprefix(prefix))

    # -- operations
    async def put(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredObject:
        payload = await _materialize(data, max_bytes)
        _validate_key(key)
        await self.ensure_bucket()
        client = await self._get_client()
        extra = {"ContentType": content_type} if content_type else {}
        async with _s3_errors():
            response = await client.put_object(Bucket=self.bucket, Key=key, Body=payload, **extra)
        return StoredObject(
            key=key,
            uri=self.uri_for(key),
            size=len(payload),
            content_type=content_type,
            etag=(response.get("ETag") or "").strip('"') or None,
        )

    async def get(self, key: str) -> bytes:
        client = await self._get_client()
        async with _s3_errors(key):
            response = await client.get_object(Bucket=self.bucket, Key=_validate_key(key))
            async with response["Body"] as body:
                return await body.read()

    async def stream(
        self, key: str, *, chunk_size: int = _DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        client = await self._get_client()
        async with _s3_errors(key):
            response = await client.get_object(Bucket=self.bucket, Key=_validate_key(key))
        body = response["Body"]
        try:
            # aiobotocore hands back either a StreamingBody (``iter_chunks``) or, when a
            # response checksum is present, a StreamingChecksumBody that only exposes the
            # underlying aiohttp reader — verified against the local MinIO, which returns
            # the latter. Support both rather than pinning one aiobotocore shape.
            if hasattr(body, "iter_chunks"):
                async for chunk in body.iter_chunks(chunk_size):
                    yield chunk
            else:
                async for chunk in body.content.iter_chunked(chunk_size):
                    yield chunk
        finally:
            body.close()

    async def delete(self, key: str) -> None:
        # S3 DELETE is idempotent: a missing key still returns 204.
        client = await self._get_client()
        async with _s3_errors():
            await client.delete_object(Bucket=self.bucket, Key=_validate_key(key))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under ``prefix`` with concurrent single-object deletes.

        Deliberately **not** ``DeleteObjects``: that operation requires a body checksum,
        and the header modern botocore sends (``x-amz-checksum-crc32``) is rejected by
        older MinIO builds, which still demand ``Content-MD5`` (verified against the
        local MinIO — it fails with ``MissingContentMD5``). Single deletes are
        universally accepted, and a document holds a handful of objects (original +
        artifacts per version), so the batch API buys nothing here.
        """
        _validate_key(prefix)
        client = await self._get_client()
        removed = 0
        paginator = client.get_paginator("list_objects_v2")
        batch: list[str] = []
        async with _s3_errors():
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    batch.append(item["Key"])
                    if len(batch) == _S3_DELETE_CONCURRENCY:
                        removed += await self._delete_many(client, batch)
                        batch = []
            if batch:
                removed += await self._delete_many(client, batch)
        return removed

    async def _delete_many(self, client, keys: list[str]) -> int:  # noqa: ANN001
        await asyncio.gather(*(client.delete_object(Bucket=self.bucket, Key=key) for key in keys))
        return len(keys)

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        client = await self._get_client()
        async with _s3_errors():
            try:
                await client.head_object(Bucket=self.bucket, Key=_validate_key(key))
            except ClientError as exc:
                if _error_code(exc) in _MISSING_CODES:
                    return False
                raise
        return True

    async def check_health(self) -> None:
        """Verify the bucket is reachable *and* the credentials work (``HeadBucket``)."""
        client = await self._get_client()
        async with _s3_errors():
            await client.head_bucket(Bucket=self.bucket)


# --- factory / DI ------------------------------------------------------------

_storage: ObjectStorage | None = None


def build_object_storage(settings: Settings | None = None) -> ObjectStorage:
    """Construct the backend named by ``STORAGE_BACKEND`` (no caching)."""
    settings = settings or get_settings()
    if settings.storage.backend == "local":
        return LocalFilesystemStorage(settings.storage.local_root)
    return S3ObjectStorage(settings)


def get_object_storage() -> ObjectStorage:
    """Process-wide object storage — also usable as a FastAPI dependency.

    The instance is cached because the S3 client pools connections and is bound to the
    running event loop; :func:`close_object_storage` (app lifespan / test teardown)
    tears it down.
    """
    global _storage
    if _storage is None:
        _storage = build_object_storage()
    return _storage


async def close_object_storage() -> None:
    """Close and forget the cached instance."""
    global _storage
    if _storage is not None:
        await _storage.aclose()
        _storage = None


#: Route dependency for the upload/delete surfaces (T-202, T-208/T-209), following the
#: `Annotated` DI convention established in `app.auth.dependencies`.
ObjectStorageDep = Annotated[ObjectStorage, Depends(get_object_storage)]
