"""OCR sidecar client — the FR-ING-07 recognition seam (T-217, R-88 §8.78).

NFR-SEC-09 puts recognition **outside** the worker process: rasterisation stays in the
worker (R-31 unchanged — T-218 calls `get_pixmap`), and the page raster crosses a local
socket to a container holding the image decoders and the engine, so a memory-safety defect
there cannot reach this process's database session or its object-storage credentials. This
module is the near side of that socket, and — exactly as R-32's in-tree `clamd` client did —
it adds **no package to the lock file**: `httpx` is already declared.

**Why this lives under `app/services/` and not `app/ingestion/`.** `app.services.health`
probes the sidecar for `/health/ready/worker`, and the API process must be able to do that
without importing a parser (which drags in PyMuPDF). Same split, same reason, as
`app.services.clamav` beside `app.ingestion.scanner`.

**This client is SYNCHRONOUS, and it is the one place in the codebase that is.** Every other
HTTP client here is `httpx.AsyncClient`, but the caller is not async code: `parse_document`
is `asyncio.to_thread(parse_document_sync, ...)`, so T-218's page loop reaches this from a
worker thread which — as `app.config` already records — cannot be cancelled. A blocking call
on a thread that exists to block is the honest shape; bouncing a coroutine back to the loop
with `run_coroutine_threadsafe` from a non-cancellable thread is strictly worse. The
readiness probe wraps :meth:`OcrClient.ping` in `asyncio.to_thread` for the same reason,
from the other side.

**The error tree deliberately carries no `code`/`retryable` ClassVars**, unlike its siblings
in `app.ingestion.parsers`, `app.services.embeddings` and `app.services.clamav`. That
convention exists so `workers.ingest` can write `knowledge_jobs.error_code` and fail a job.
R-88(9) makes recognition fail **open** — a sidecar that is down, slow or erroring degrades
to whatever text extraction produced, which is the behaviour that shipped for a year — so no
failure raised here ever reaches that path, and giving it the shape of one would invite a
future caller to treat it as terminal. The document-level `NO_EXTRACTABLE_TEXT` still fails
closed, which is what preserves R-34's guarantee that no zero-chunk document goes `ACTIVE`.

**One failure must never look like a result.** A malformed or truncated response raises
rather than degrading to empty text — the same trap R-32 closed on the other client, where
`clamd` answers an oversized stream by truncating and reporting the prefix *clean*. Here
"recognised nothing" is a legitimate outcome for a blank page, so if "the call failed" were
also an empty result the two would be indistinguishable, and a broken sidecar would look
like a corpus of blank pages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import structlog

from app.config import OcrSettings, Settings, get_settings

log = structlog.get_logger(__name__)

#: The sidecar reads the language from this header rather than the query string or a JSON
#: envelope, so the request body is exactly the raster and nothing has to be parsed to find
#: where the image starts.
LANGUAGE_HEADER = "X-Ocr-Lang"

_RECOGNIZE_PATH = "/recognize"
_HEALTH_PATH = "/health"


class OcrError(Exception):
    """Base for recognition failures. Always fails open at the call site (R-88(9))."""


class OcrUnavailableError(OcrError):
    """The sidecar could not be reached, timed out, or died mid-request."""


class OcrProtocolError(OcrError):
    """The sidecar answered with something this client does not understand."""


class OcrImageTooLargeError(OcrError):
    """The raster exceeds ``OCR_MAX_IMAGE_BYTES`` — refused before the request is made."""


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One recognised word and the engine's confidence in it, 0–100."""

    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """Everything one recognition pass produced.

    `text` is the only field that reaches a chunk, and therefore the only one R-88(1)'s
    determinism argument is about: it feeds `chunk_text`, which feeds `embedding_fingerprint`.
    """

    text: str
    words: tuple[OcrWord, ...]
    engine_version: str
    languages: tuple[str, ...]

    @property
    def mean_confidence(self) -> float | None:
        """Mean per-word confidence, or ``None`` when nothing was recognised.

        R-88(8) discards a page whose mean confidence falls below the floor, so this is the
        number that decides whether recognised text is stored at all — which is why the
        aggregation lives here, in tested code, rather than inside the container.

        Two rules, both load-bearing. Words the engine could not score (Tesseract writes
        ``conf = -1`` on structural rows) are **excluded** rather than counted as zero, or a
        page's mean would be dragged down by rows that are not words. And a page with no
        scored words answers ``None`` rather than ``0.0``: 0.0 is a real confidence value
        meaning "recognised, and garbage", while `None` means "nothing was recognised" —
        a blank page is not a garbled one, and a caller comparing against a floor should be
        made to decide about the difference rather than have it decided silently.
        """
        # `.strip()`, not truthiness: a whitespace-only token is not a recognised word, and
        # scoring one would let layout contribute to a number that decides whether the page
        # is stored. The sidecar drops them too — this is the half that is unit-tested.
        scored = [
            word.confidence for word in self.words if word.confidence >= 0 and word.text.strip()
        ]
        if not scored:
            return None
        return sum(scored) / len(scored)


class OcrClient:
    """Blocking HTTP client for the OCR sidecar.

    The `httpx.Client` is created lazily and reused: it is documented thread-safe, and the
    worker may parse two documents concurrently, so a pooled client is both correct and what
    keeps the connection from being re-established once per page.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        self._settings: OcrSettings = resolved.ocr
        self._client: httpx.Client | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self._settings.host}:{self._settings.port}"

    @property
    def languages(self) -> str:
        return self._settings.languages

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.endpoint)
        return self._client

    def ping(self) -> None:
        """Raise unless the sidecar reports itself healthy. Backs the readiness probe."""
        payload = self._request(
            "GET", _HEALTH_PATH, budget_seconds=self._settings.ping_timeout_seconds
        )
        if payload.get("status") != "ok":
            raise OcrProtocolError(f"{self.endpoint} did not report status=ok: {payload!r}")

    def recognize(self, image: bytes) -> OcrPageResult:
        """Recognise one page raster (PNG bytes rendered by the caller).

        There is deliberately no per-call language override. The language reaches the
        sidecar's ``argv``, and ``OcrSettings`` validates it at the settings boundary — so
        taking it from a parameter would reintroduce an unvalidated path to that argv for a
        caller that does not exist: nothing in FR-ING-07 selects a language per page.
        """
        if len(image) > self._settings.max_image_bytes:
            # Refused *before* the request: a page rendered at an absurd DPI should cost no
            # transfer, and the sidecar's own 413 is the backstop rather than the control.
            raise OcrImageTooLargeError(
                f"{len(image):,} bytes exceeds OCR_MAX_IMAGE_BYTES "
                f"({self._settings.max_image_bytes:,})"
            )
        payload = self._request(
            "POST",
            _RECOGNIZE_PATH,
            budget_seconds=self._settings.timeout_seconds,
            content=image,
            headers={
                "Content-Type": "image/png",
                LANGUAGE_HEADER: self._settings.languages,
            },
        )
        return _interpret(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        budget_seconds: float,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._http().request(
                method, path, content=content, headers=headers, timeout=budget_seconds
            )
        except httpx.TimeoutException as exc:
            raise OcrUnavailableError(
                f"OCR sidecar at {self.endpoint} did not answer within {budget_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            # ConnectError, ReadError, RemoteProtocolError — all HTTPError.
            raise OcrUnavailableError(f"OCR sidecar at {self.endpoint} unreachable: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise OcrProtocolError(
                f"OCR sidecar at {self.endpoint}{path} answered {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            payload = json.loads(response.content)
        except ValueError as exc:
            raise OcrProtocolError(
                f"OCR sidecar at {self.endpoint}{path} answered non-JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise OcrProtocolError(
                f"OCR sidecar at {self.endpoint}{path} answered "
                f"{type(payload).__name__}, not an object"
            )
        return payload

    def close(self) -> None:
        """Release the pooled connections. Sync, because the client is."""
        if self._client is not None:
            self._client.close()
            self._client = None


def _interpret(payload: dict[str, object]) -> OcrPageResult:
    """Map a `/recognize` body onto :class:`OcrPageResult`, or raise.

    Every field is checked rather than defaulted. A missing `text` is not an empty page and
    a missing `words` list is not an unscored one — both mean this is not the sidecar, or not
    the sidecar we think, and the caller must be able to tell that from a blank scan.
    """
    text = payload.get("text")
    raw_words = payload.get("words")
    engine = payload.get("engine")
    if not isinstance(text, str) or not isinstance(raw_words, list) or not isinstance(engine, dict):
        raise OcrProtocolError(f"unrecognised OCR response shape: {sorted(payload)!r}")

    words: list[OcrWord] = []
    for entry in raw_words:
        if not isinstance(entry, dict):
            raise OcrProtocolError(f"unrecognised OCR word entry: {entry!r}")
        word_text = entry.get("text")
        confidence = entry.get("conf")
        if not isinstance(word_text, str) or not isinstance(confidence, int | float):
            raise OcrProtocolError(f"unrecognised OCR word entry: {entry!r}")
        words.append(OcrWord(text=word_text, confidence=float(confidence)))

    version = engine.get("tesseract")
    langs = engine.get("langs")
    return OcrPageResult(
        text=text,
        words=tuple(words),
        engine_version=version if isinstance(version, str) else "",
        languages=tuple(lang for lang in langs if isinstance(lang, str))
        if isinstance(langs, list)
        else (),
    )


# ---- factory / DI ------------------------------------------------------------------

_client: OcrClient | None = None


def build_ocr_client(settings: Settings | None = None) -> OcrClient:
    """Construct a client without touching the process-wide cache (tests, worker startup)."""
    return OcrClient(settings)


def get_ocr_client() -> OcrClient:
    """Return the process-wide client, building it on first use."""
    global _client
    if _client is None:
        _client = build_ocr_client()
    return _client


async def close_ocr_client() -> None:
    """Release the cached client.

    Async only so the arq `on_shutdown` hook closes every seam the same way; the close itself
    is synchronous because the transport is.
    """
    global _client
    if _client is not None:
        _client.close()
        _client = None
