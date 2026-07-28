"""ClamAV `clamd` client — the INSTREAM signature screen (T-207, R-32 §8.13).

R-32 selects ClamAV as the signature engine and is explicit that the client is **a small
in-tree async one, not the PyPI `clamd`/`pyclamd` packages**: both are synchronous and long
unmaintained, while the protocol is trivial (``zINSTREAM\\0``, length-prefixed chunks, a
zero-length terminator). Writing it here avoids a stale dependency *and* the `to_thread`
wrapper a sync SDK would force into an otherwise fully async worker — the same reasoning
T-104 applied when it extended an in-house httpx Keycloak client rather than adopting
`python-keycloak`. This module adds **no package** to the lock file.

**Why this lives under `app/services/` and not `app/ingestion/`.** `app.services.health`
imports it for the `/health/ready/worker` clamd probe (R-38(2)). The screening *policy* —
which lives in `app.ingestion.scanner` — pulls in PyMuPDF and zipfile for the structural
checks, and R-31 keeps that out of the API process entirely. Splitting the socket client
from the policy is what lets the API probe clamd without importing a parser.

**The fail-open trap this module exists to close.** clamd's default ``StreamMaxLength`` is
25 MB. Hand it more than that and it does not error out of the scan — it truncates the
stream and reports the *prefix* clean. Against Corpus's 50 MB ceiling (FR-ERR-01) that is
a silent bypass for exactly the large files most worth scanning. Two defences, because
neither alone is sufficient:

* No INSTREAM command reports the daemon's configured limit, so a misconfigured `clamd`
  cannot be detected at runtime. :class:`ClamAVClient` therefore refuses to stream a
  payload above ``CLAMAV_MAX_STREAM_BYTES`` instead of risking the truncation, and
  :class:`app.config.Settings` refuses to boot when that value is below
  ``UPLOAD_MAX_FILE_BYTES``.
* ``deployment/clamav/clamd.conf`` raises the daemon's own ``StreamMaxLength``.

And if the daemon reports the limit anyway (a conf drift), the ``ERROR`` reply is mapped to
an exception — **never** to a clean verdict.

Errors follow the `code`/`retryable` `ClassVar` convention T-203 and T-205 established, so
`workers.ingest` classifies scanner, parse, embedding and incremental failures through one
code path. Everything here is retryable **except** an oversized payload, because R-32 makes
scanner unavailability fail the job *closed* — the deliberate opposite of the fail-open
rate limiter (T-105), where the risk runs the other way.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.config import ClamAVSettings, Settings, get_settings

log = structlog.get_logger(__name__)

# `z`-prefixed commands are NUL-terminated (the `n`-prefixed form is newline-terminated).
# clamd requires one or the other; a bare `INSTREAM` is deprecated and ambiguous.
_CMD_INSTREAM = b"zINSTREAM\0"
_CMD_PING = b"zPING\0"

_TERMINATOR = struct.pack("!L", 0)
# clamd's replies are short; anything longer means we are not talking to clamd.
_MAX_REPLY_BYTES = 4096


class ClamAVError(Exception):
    """Base for `clamd` failures.

    `code` is what `workers.ingest` writes to `knowledge_jobs.error_code`; `str(exc)` is
    the human message for `documents.error_message`.

    **`retryable` defaults to True here, unlike every sibling taxonomy.** R-32 requires an
    unreachable or misbehaving scanner to fail the job *closed* — retried, and eventually
    dead-lettered — rather than admitting an unscanned document. So the safe default for
    an unrecognised scanner failure is "try again", not "give up and parse it anyway".
    """

    code: ClassVar[str] = "SCANNER_FAILED"
    retryable: ClassVar[bool] = True


class ClamAVUnavailableError(ClamAVError):
    """`clamd` could not be reached, or died mid-scan."""

    code: ClassVar[str] = "SCANNER_UNAVAILABLE"
    retryable: ClassVar[bool] = True


class ClamAVProtocolError(ClamAVError):
    """`clamd` answered with something this client does not understand.

    Retryable on purpose. The likely causes are a truncated reply or a port pointed at the
    wrong service — neither is a reason to let an unscanned document through, and a genuine
    misconfiguration surfaces as a dead-lettered job plus a red `/health/ready/worker`.
    """

    code: ClassVar[str] = "SCANNER_PROTOCOL_ERROR"
    retryable: ClassVar[bool] = True


class ClamAVPayloadTooLargeError(ClamAVError):
    """The payload exceeds ``CLAMAV_MAX_STREAM_BYTES`` — refused rather than truncated.

    The one terminal member of this tree: a retry cannot make the file smaller. Reaching
    it means the boot-time `Settings` invariant was bypassed (a payload larger than
    ``UPLOAD_MAX_FILE_BYTES`` got into storage), so it is closer to an invariant breach
    than a scan outcome.
    """

    code: ClassVar[str] = "SCANNER_PAYLOAD_TOO_LARGE"
    retryable: ClassVar[bool] = False


@dataclass(frozen=True, slots=True)
class ClamAVScanReport:
    """Outcome of one INSTREAM scan. `signature` is set only when `infected` is True."""

    infected: bool
    signature: str | None = None


class ClamAVClient:
    """Async `clamd` client over a plain TCP socket.

    Stateless by design: clamd closes the connection after answering an INSTREAM, so
    pooling buys nothing and a per-scan connection keeps a dead daemon from poisoning a
    cached handle. There is consequently no event-loop binding here — unlike the S3 and
    OpenAI clients — but the `build_/get_/close_` trio is kept for symmetry with the other
    seams and so the worker has one place to construct it.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        self._settings: ClamAVSettings = resolved.clamav

    @property
    def endpoint(self) -> str:
        return f"{self._settings.host}:{self._settings.port}"

    async def ping(self) -> None:
        """Raise unless `clamd` answers `PONG`. Backs the readiness probe (R-38(2))."""
        reply = await self._exchange(
            _CMD_PING, payload=None, budget_seconds=self._settings.ping_timeout_seconds
        )
        if reply != "PONG":
            raise ClamAVProtocolError(f"expected PONG from {self.endpoint}, got {reply!r}")

    async def scan(self, payload: bytes) -> ClamAVScanReport:
        """Stream `payload` to `clamd` and interpret the verdict."""
        if len(payload) > self._settings.max_stream_bytes:
            # Refuse rather than let clamd truncate and call the prefix clean.
            raise ClamAVPayloadTooLargeError(
                f"{len(payload):,} bytes exceeds CLAMAV_MAX_STREAM_BYTES "
                f"({self._settings.max_stream_bytes:,}); refusing to stream a payload "
                "clamd may silently truncate"
            )
        reply = await self._exchange(
            _CMD_INSTREAM, payload=payload, budget_seconds=self._settings.timeout_seconds
        )
        return _interpret(reply)

    # ---- transport ---------------------------------------------------------------

    async def _exchange(
        self, command: bytes, *, payload: bytes | None, budget_seconds: float
    ) -> str:
        # Named `budget_seconds` rather than `timeout` only to keep ASYNC109 quiet: that
        # rule exists to catch hand-rolled timeout plumbing, and this *is* `asyncio.timeout`.
        writer: asyncio.StreamWriter | None = None
        interrupted = False
        try:
            async with asyncio.timeout(budget_seconds):
                reader, writer = await asyncio.open_connection(
                    self._settings.host,
                    self._settings.port,
                    # Bounds the read buffer so a service that is not clamd (a wrong port
                    # pointed at something chatty) raises rather than buffering forever.
                    limit=_MAX_REPLY_BYTES,
                )
                writer.write(command)
                await writer.drain()
                if payload is not None:
                    interrupted = await self._stream(writer, payload)
                try:
                    raw = await _read_reply(reader)
                except OSError:
                    # An abortive close (Windows sends RST) discards anything the daemon
                    # had already put on the wire, so after an interrupted write there may
                    # be no reply left to read. Fall through to the diagnosis below rather
                    # than letting this look like a connection failure.
                    if not interrupted:
                        raise
                    raw = b""
        except TimeoutError as exc:
            raise ClamAVUnavailableError(
                f"clamd at {self.endpoint} did not answer within {budget_seconds}s"
            ) from exc
        except ClamAVError:
            raise
        except OSError as exc:
            # ConnectionRefusedError, gaierror, ConnectionResetError — all OSError.
            raise ClamAVUnavailableError(f"clamd at {self.endpoint} unreachable: {exc}") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    # The exchange has already produced its result or its exception; a
                    # failure to close cleanly must not mask either.
                    pass

        if not raw:
            if interrupted:
                # clamd hung up mid-upload and the reply, if it sent one, was lost with the
                # connection. The overwhelmingly likely cause is that it rejected the
                # stream — `StreamMaxLength` being the one that matters, since exceeding it
                # is precisely how clamd fails *open* (R-32). Reporting this as
                # "unreachable" would send an operator hunting a network fault while the
                # real problem is one line of clamd.conf, so it is classified as a protocol
                # error and says so. Still retryable: an unverified document must not pass.
                raise ClamAVProtocolError(
                    f"clamd at {self.endpoint} closed the connection while the file was "
                    "still uploading and sent no verdict — it most likely rejected the "
                    "stream; check StreamMaxLength against CLAMAV_MAX_STREAM_BYTES"
                )
            raise ClamAVUnavailableError(
                f"clamd at {self.endpoint} closed the connection without replying"
            )
        return raw.rstrip(b"\0").decode("utf-8", errors="replace").strip()

    async def _stream(self, writer: asyncio.StreamWriter, payload: bytes) -> bool:
        """Write length-prefixed chunks then the terminator. True if the daemon hung up.

        clamd can answer *early* — most importantly with the size-limit error — and close
        the socket while we are still writing, which surfaces as a broken pipe here. That
        is swallowed so the caller can still read the reply that caused it: where the close
        is graceful the real message survives. Where it is abortive (Windows sends RST,
        discarding anything already received) there is nothing left to read, and the
        returned flag is what lets the caller diagnose it correctly rather than report a
        phantom network failure.
        """
        chunk_size = self._settings.chunk_bytes
        try:
            for start in range(0, len(payload), chunk_size):
                chunk = payload[start : start + chunk_size]
                writer.write(struct.pack("!L", len(chunk)))
                writer.write(chunk)
                await writer.drain()
            writer.write(_TERMINATOR)
            await writer.drain()
        except ConnectionResetError, BrokenPipeError:
            log.debug("clamav.stream_interrupted", endpoint=self.endpoint, size=len(payload))
            return True
        return False

    async def aclose(self) -> None:
        """No pooled resources — present so the seam matches the other service clients."""
        return None


async def _read_reply(reader: asyncio.StreamReader) -> bytes:
    """Read one NUL-terminated `clamd` reply.

    A bare ``read(n)`` would be a latent bug: it returns whatever has arrived so far, so a
    reply split across TCP segments could be interpreted from its prefix — and the prefix
    of ``stream: Eicar-Test-Signature FOUND`` is ``stream: `` , which matches nothing and
    would surface as a protocol error rather than a detection. Read to the delimiter.
    """
    try:
        return await reader.readuntil(b"\0")
    except asyncio.IncompleteReadError as exc:
        # clamd closed without the terminator. Whatever arrived is still the best
        # diagnostic we have; an empty partial is handled by the caller.
        return exc.partial
    except asyncio.LimitOverrunError as exc:
        raise ClamAVProtocolError(
            f"clamd reply exceeded {_MAX_REPLY_BYTES} bytes without a terminator: {exc}"
        ) from exc


def _interpret(reply: str) -> ClamAVScanReport:
    """Map a clamd INSTREAM reply to a verdict.

    Shapes: ``stream: OK`` · ``stream: <Signature> FOUND`` · ``... ERROR`` (notably
    ``INSTREAM size limit exceeded. ERROR``).
    """
    if reply.endswith("ERROR"):
        # Never a clean verdict. `size limit exceeded` in particular means clamd read only
        # a prefix — treating that as OK is precisely the fail-open R-32 forbids.
        raise ClamAVProtocolError(f"clamd reported an error: {reply}")
    if reply.endswith("FOUND"):
        body = reply[: -len("FOUND")].strip()
        _, _, signature = body.partition(":")
        return ClamAVScanReport(infected=True, signature=signature.strip() or body or None)
    if reply.endswith("OK"):
        return ClamAVScanReport(infected=False)
    raise ClamAVProtocolError(f"unrecognised clamd reply: {reply!r}")


# ---- factory / DI ------------------------------------------------------------------

_client: ClamAVClient | None = None


def build_clamav_client(settings: Settings | None = None) -> ClamAVClient:
    """Construct a client without touching the process-wide cache (tests, worker startup)."""
    return ClamAVClient(settings)


def get_clamav_client() -> ClamAVClient:
    """Return the process-wide client, building it on first use."""
    global _client
    if _client is None:
        _client = build_clamav_client()
    return _client


async def close_clamav_client() -> None:
    """Release the cached client. Called from the arq `on_shutdown` hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
