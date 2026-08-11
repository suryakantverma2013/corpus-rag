"""Google Drive adapter — list and fetch, and nothing else (T-214, FR-KBM-10, R-63).

The **only** provider-specific code in the application. Everything else about cloud import is
the ordinary ingestion path: R-63 makes an import a one-time copy, so FR-KBM-08 dedup, the
FR-ERR-02 quota, the 50 MB limit, format validation and R-32 scanning all apply unchanged and
an imported document is thereafter indistinguishable from an uploaded one.

**No query ever reaches Google.** This is an alternative ingestion route, not a retrieval
backend — the distinction that R-68 was issued and vacated over (§8.52).

R-63(6)'s three constraints are what this module exists to enforce, and each is a named
function below rather than a comment:

1. **Fixed host, validated id.** The URL is built from `CLOUD_API_BASE` and a file id that
   has been matched against `_FILE_ID` — never from anything a client supplies whole. That is
   what keeps this from being the SSRF surface a generic "import from URL" would be.
2. **Size checked before the download, and the stream capped regardless.** `fetch_metadata`
   refuses an over-sized file before a byte is transferred; `DriveDownload` then caps the
   read anyway, because `size` is the provider's claim and a claim is not a guarantee.
3. **Provider bytes are untrusted exactly as uploaded bytes.** This module returns a byte
   source and makes no judgement about content — the same sniffing, validation and scanning
   run on it as on an upload, because it is handed to the same function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.config import Settings
from app.security.content_validation import MIME_BY_SUFFIX

log = structlog.get_logger(__name__)

#: The formats a user may import, derived from the ingestion module's own table rather than
#: restated — FR-ING-02's set is fixed by R-11, and a second copy here would be free to drift
#: into offering a format the parser cannot read.
IMPORTABLE_MIME_TYPES: frozenset[str] = frozenset(MIME_BY_SUFFIX.values())

#: Drive file ids are opaque, but they are not arbitrary: base64url-ish, no separators. The
#: pattern is deliberately strict because this value becomes a URL path segment — R-63(6)(1)'s
#: "validated file id" is this line.
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{6,256}$")

#: Fields asked of Drive. Naming them is not an optimisation: `files.list` returns a partial
#: representation by default, and `size` — which the pre-download check depends on — is not in
#: it. Asking for exactly what is used also means nothing else is logged or returned by
#: accident.
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime)"
_GET_FIELDS = "id,name,mimeType,size"


class DriveError(Exception):
    """The provider call failed. Never carries a token."""


class DriveUnavailableError(DriveError):
    """Drive was unreachable, timed out, or failed server-side → 503 (retryable)."""


class DriveForbiddenError(DriveError):
    """Drive rejected the brokered token, or the user cannot read this file.

    Kept apart from "unavailable" because a retry cannot fix it: the usual causes are a
    revoked Google grant or a file that has been unshared, and both need the user to act.
    """


class DriveFileNotFoundError(DriveError):
    """No such file for this user."""


class InvalidFileIdError(DriveError):
    """The id is not a Drive file id (R-63(6)(1)). Raised before any request is built."""


class UnsupportedDriveFileError(DriveError):
    """The file is not one of the four FR-ING-02 formats → 415.

    Refused here rather than downloaded and refused later: a Google Doc or a 2 GB video has
    nothing to gain from a round trip through the ingestion path that would reject it.
    """


class DriveFileTooLargeError(DriveError):
    """The **declared** size exceeds the FR-ERR-01 ceiling → 413, before any download."""


@dataclass(frozen=True, slots=True)
class DriveFile:
    """One row of the FR-KBM-10 selection surface.

    Metadata only, on `DocumentResponse`'s principle (R-31(4)): nothing here carries or points
    at bytes, and no field is a URL.
    """

    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None


@dataclass(frozen=True, slots=True)
class DriveListing:
    files: list[DriveFile]
    next_page_token: str | None


def validate_file_id(file_id: str) -> str:
    """R-63(6)(1). Raise rather than escape: an id that needs escaping is not an id."""
    if not _FILE_ID.match(file_id):
        raise InvalidFileIdError("not a Drive file id")
    return file_id


def _build_query(search: str | None) -> str:
    """Drive's `q`, filtered to the importable formats server-side.

    Server-side, not after the fact, for the reason FR-KBM-10 gives for having no folder tree:
    only four formats are ingestible, so a client-side filter would page through a user's
    whole Drive to display a handful of rows.

    The search term is embedded in a quoted `contains` clause, so the quote and backslash are
    escaped — the same discipline as the file id, one syntax along.
    """
    formats = " or ".join(f"mimeType = '{m}'" for m in sorted(IMPORTABLE_MIME_TYPES))
    clauses = ["trashed = false", f"({formats})"]
    if search:
        escaped = search.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{escaped}'")
    return " and ".join(clauses)


def _translate(exc: httpx.HTTPError) -> DriveError:
    return DriveUnavailableError(str(exc))


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code in (401, 403):
        # A brokered token Google refuses. Keycloak still holds a link, so this is not
        # "unlinked" — the user revoked access at Google's end, or cannot read this file.
        raise DriveForbiddenError(f"drive returned {resp.status_code}")
    if resp.status_code == 404:
        raise DriveFileNotFoundError("no such file")
    raise DriveUnavailableError(f"drive returned {resp.status_code}")


async def list_files(
    *,
    access_token: str,
    settings: Settings,
    search: str | None = None,
    page_token: str | None = None,
    page_size: int | None = None,
) -> DriveListing:
    """The FR-KBM-10 flat list, filtered to the four ingestible formats.

    Flat by requirement, not by omission: with only four importable formats a folder tree
    would mostly display files the user cannot pick.
    """
    params: dict[str, Any] = {
        "q": _build_query(search),
        "fields": _LIST_FIELDS,
        "pageSize": min(
            page_size or settings.cloud.list_page_size, settings.cloud.max_list_page_size
        ),
        "orderBy": "modifiedTime desc",
        # Without these two a user whose documents live in a shared drive sees an empty list
        # and no explanation.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        async with httpx.AsyncClient(timeout=settings.cloud.timeout_seconds) as client:
            resp = await client.get(
                f"{settings.cloud.api_base.rstrip('/')}/drive/v3/files",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise _translate(exc) from exc

    _raise_for_status(resp)
    body = resp.json()
    return DriveListing(
        files=[_to_file(item) for item in body.get("files", [])],
        next_page_token=body.get("nextPageToken"),
    )


def _to_file(item: dict[str, Any]) -> DriveFile:
    size = item.get("size")
    return DriveFile(
        file_id=str(item.get("id", "")),
        name=str(item.get("name", "")),
        mime_type=str(item.get("mimeType", "")),
        # Absent for Google-native documents, which the mime filter already excludes — but
        # read defensively, because "the filter guarantees it" is how a `None` becomes a 500.
        size_bytes=int(size) if size is not None else None,
        modified_time=item.get("modifiedTime"),
    )


async def fetch_metadata(*, file_id: str, access_token: str, settings: Settings) -> DriveFile:
    """One file's metadata — and R-63(6)(2)'s **pre-download** half.

    Both refusals happen here, before a byte moves: an unsupported format and an over-sized
    file. The size ceiling is re-applied to the stream regardless (see `DriveDownload`),
    because this number is Drive's claim about the file, not a property of the bytes.
    """
    validate_file_id(file_id)
    try:
        async with httpx.AsyncClient(timeout=settings.cloud.timeout_seconds) as client:
            resp = await client.get(
                f"{settings.cloud.api_base.rstrip('/')}/drive/v3/files/{file_id}",
                params={"fields": _GET_FIELDS, "supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise _translate(exc) from exc

    _raise_for_status(resp)
    metadata = _to_file(resp.json())

    if metadata.mime_type not in IMPORTABLE_MIME_TYPES:
        raise UnsupportedDriveFileError(metadata.mime_type)
    if metadata.size_bytes is not None and metadata.size_bytes > settings.upload.max_file_bytes:
        raise DriveFileTooLargeError(f"declared {metadata.size_bytes} bytes")
    return metadata


class DriveDownload:
    """A Drive file as a byte source the ingestion path can read (R-63(6)(3)).

    Structurally an `UploadFile` as far as `upload_document` is concerned — `filename` plus an
    async `read(size)` — which is the whole point: the import is handed to the *same* function
    an upload is, so there is no second code path to keep in step and no second place for a
    validation rule to be forgotten.

    **The cap here is the one that is not advisory.** `fetch_metadata` has already refused an
    over-sized *declared* size; this refuses over-sized *bytes*, which is what a misreported
    size would produce. In practice `_read_and_hash` trips its own identical ceiling first —
    deliberately, so the error the user sees is the ordinary FR-ERR-01 `413` rather than a
    provider-flavoured variant of it. This cap is what makes that true even if the caller
    changes.
    """

    __slots__ = ("filename", "_response", "_client", "_iterator", "_buffer", "_read", "_max_bytes")

    def __init__(
        self,
        *,
        filename: str,
        client: httpx.AsyncClient,
        response: httpx.Response,
        max_bytes: int,
    ) -> None:
        self.filename = filename
        self._client = client
        self._response = response
        self._iterator = response.aiter_bytes()
        self._buffer = bytearray()
        self._read = 0
        self._max_bytes = max_bytes

    async def read(self, size: int = -1) -> bytes:
        """Return up to `size` bytes, or everything remaining when `size` is negative.

        `UploadFile.read` semantics, because that is what the caller was written against.
        """
        while size < 0 or len(self._buffer) < size:
            try:
                chunk = await anext(self._iterator)
            except StopAsyncIteration:
                break
            except httpx.HTTPError as exc:
                raise _translate(exc) from exc
            self._read += len(chunk)
            if self._read > self._max_bytes:
                raise DriveFileTooLargeError(f"stream exceeded {self._max_bytes} bytes")
            self._buffer.extend(chunk)

        if size < 0:
            taken, self._buffer = bytes(self._buffer), bytearray()
            return taken
        taken = bytes(self._buffer[:size])
        del self._buffer[:size]
        return taken

    async def aclose(self) -> None:
        await self._response.aclose()
        await self._client.aclose()

    async def __aenter__(self) -> DriveDownload:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


async def open_download(
    *, metadata: DriveFile, access_token: str, settings: Settings
) -> DriveDownload:
    """Open the byte stream for a file whose metadata has already been accepted.

    Takes the metadata rather than an id so the R-63(6)(2) pre-checks cannot be skipped by
    calling this directly — the type is the reminder.
    """
    file_id = validate_file_id(metadata.file_id)
    client = httpx.AsyncClient(timeout=settings.cloud.timeout_seconds)
    request = client.build_request(
        "GET",
        f"{settings.cloud.api_base.rstrip('/')}/drive/v3/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise _translate(exc) from exc

    if response.status_code != 200:
        # The body must be drained before the status can be reported, or httpx raises about
        # an unread stream while we are already handling an error.
        await response.aread()
        await response.aclose()
        await client.aclose()
        _raise_for_status(response)

    return DriveDownload(
        filename=metadata.name,
        client=client,
        response=response,
        max_bytes=settings.upload.max_file_bytes,
    )


__all__ = [
    "IMPORTABLE_MIME_TYPES",
    "DriveDownload",
    "DriveError",
    "DriveFile",
    "DriveFileNotFoundError",
    "DriveFileTooLargeError",
    "DriveForbiddenError",
    "DriveListing",
    "DriveUnavailableError",
    "InvalidFileIdError",
    "UnsupportedDriveFileError",
    "fetch_metadata",
    "list_files",
    "open_download",
    "validate_file_id",
]
