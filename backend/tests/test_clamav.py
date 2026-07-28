"""The in-tree ClamAV INSTREAM client (T-207, R-32 §8.13).

Tested against a **real socket** — a throwaway `asyncio` server standing in for `clamd` —
rather than a mocked reader/writer pair. The whole risk in this module is the wire framing
(length-prefixed chunks, the zero-length terminator, NUL-terminated replies), and a mock of
`StreamReader` would happily agree with whatever framing the client invented.

The load-bearing test here is `test_a_size_limit_error_is_never_reported_as_clean`: clamd
answers an oversized stream by truncating and reporting the *prefix* clean, which is the
silent fail-open R-32 exists to close.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Callable

import pytest

from app.config import ClamAVSettings, Settings, UploadSettings
from app.services.clamav import (
    ClamAVClient,
    ClamAVPayloadTooLargeError,
    ClamAVProtocolError,
    ClamAVUnavailableError,
    build_clamav_client,
    close_clamav_client,
    get_clamav_client,
)


class _FakeClamd:
    """A socket server that speaks just enough of the protocol to test the client."""

    def __init__(self) -> None:
        self.received = bytearray()
        self.commands: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None
        self.handler: Callable[[bytes], list[bytes]] | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        command = await reader.readuntil(b"\0")
        self.commands.append(command)

        if command == b"zINSTREAM\0":
            await self._read_stream(reader)

        assert self.handler is not None
        for part in self.handler(bytes(self.received)):
            writer.write(part)
            await writer.drain()
            # A deliberate gap between parts, so a reply split across two TCP writes is
            # genuinely split when the client reads it.
            await asyncio.sleep(0)
        writer.close()

    async def _read_stream(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                header = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                return
            (size,) = struct.unpack("!L", header)
            if size == 0:
                return  # the terminator
            self.received += await reader.readexactly(size)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def clamd() -> AsyncIterator[_FakeClamd]:
    server = _FakeClamd()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _client(port: int, **overrides: object) -> ClamAVClient:
    clamav = ClamAVSettings(host="127.0.0.1", port=port, **overrides)
    # The R-38(6) cross-group invariant is asserted directly below; here it just has to be
    # satisfiable, so the upload ceiling follows whatever the test asked for.
    upload = UploadSettings(max_file_bytes=min(50 * 1024 * 1024, clamav.max_stream_bytes))
    return ClamAVClient(Settings(clamav=clamav, upload=upload))


def _replies(*parts: bytes) -> Callable[[bytes], list[bytes]]:
    return lambda _payload: list(parts)


# --- verdicts ---------------------------------------------------------------------


async def test_a_clean_stream_reports_clean(clamd: _FakeClamd) -> None:
    clamd.handler = _replies(b"stream: OK\0")
    report = await _client(clamd.port).scan(b"an ordinary document")
    assert report.infected is False
    assert report.signature is None


async def test_a_detection_reports_the_signature(clamd: _FakeClamd) -> None:
    clamd.handler = _replies(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
    report = await _client(clamd.port).scan(b"payload")
    assert report.infected is True
    assert report.signature == "Win.Test.EICAR_HDB-1"


async def test_a_size_limit_error_is_never_reported_as_clean(clamd: _FakeClamd) -> None:
    """The R-32 fail-open trap: an ERROR reply must raise, never resolve to CLEAN.

    clamd answers a stream longer than `StreamMaxLength` by truncating it and scanning
    only the prefix. If this mapped to a clean verdict, a 50 MB upload against a default
    25 MB daemon would sail through unscanned and nothing would say so.
    """
    clamd.handler = _replies(b"INSTREAM size limit exceeded. ERROR\0")
    with pytest.raises(ClamAVProtocolError, match="size limit exceeded"):
        await _client(clamd.port).scan(b"payload")


async def test_an_unrecognised_reply_raises(clamd: _FakeClamd) -> None:
    clamd.handler = _replies(b"stream: something new\0")
    with pytest.raises(ClamAVProtocolError):
        await _client(clamd.port).scan(b"payload")


# --- framing ----------------------------------------------------------------------


async def test_the_payload_arrives_byte_for_byte_across_many_chunks(clamd: _FakeClamd) -> None:
    """Proves the length-prefixed framing, which is the entire point of an in-tree client."""
    clamd.handler = _replies(b"stream: OK\0")
    payload = bytes(range(256)) * 400  # 102,400 bytes — many chunks at 4 KiB

    await _client(clamd.port, chunk_bytes=4096).scan(payload)

    assert clamd.received == payload
    assert clamd.commands == [b"zINSTREAM\0"]


async def test_a_reply_split_across_two_writes_is_read_whole(clamd: _FakeClamd) -> None:
    """A single `read(n)` would see only `stream: ` here and misreport a detection."""
    clamd.handler = _replies(b"stream: Eicar-Test-", b"Signature FOUND\0")
    report = await _client(clamd.port).scan(b"payload")
    assert report.infected is True
    assert report.signature == "Eicar-Test-Signature"


async def test_a_daemon_that_rejects_a_stream_mid_upload_is_not_reported_as_unreachable() -> None:
    """clamd answering and hanging up mid-upload must diagnose as a *protocol* problem.

    This is what a `StreamMaxLength` misconfiguration actually looks like on the wire, and
    getting the classification wrong is expensive: "unreachable" sends an operator hunting
    a network fault while the real fix is one line of clamd.conf.

    Whether the daemon's reply survives is platform-dependent — Windows sends an abortive
    RST that discards it, POSIX usually delivers it — so the assertion is on the error
    *class*, which is the part the worker and the operator both act on.
    """

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\0")
        writer.write(b"INSTREAM size limit exceeded. ERROR\0")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ClamAVProtocolError) as caught:
            await _client(port, chunk_bytes=1024).scan(b"x" * (2 * 1024 * 1024))
    finally:
        server.close()
        await server.wait_closed()

    assert not isinstance(caught.value, ClamAVUnavailableError)
    assert caught.value.retryable is True  # fail closed either way (R-32)
    assert "StreamMaxLength" in str(caught.value) or "size limit" in str(caught.value)


# --- availability -----------------------------------------------------------------


async def test_an_unreachable_daemon_is_retryable() -> None:
    """R-32 fails the job *closed*: unreachable means retry, never 'scan skipped'."""
    # Port 1 is reserved and never listening.
    with pytest.raises(ClamAVUnavailableError) as caught:
        await _client(1).scan(b"payload")
    assert caught.value.retryable is True
    assert caught.value.code == "SCANNER_UNAVAILABLE"


async def test_a_daemon_that_hangs_up_without_replying_is_unavailable(
    clamd: _FakeClamd,
) -> None:
    clamd.handler = _replies()
    with pytest.raises(ClamAVUnavailableError, match="without replying"):
        await _client(clamd.port).scan(b"payload")


async def test_a_slow_daemon_times_out(clamd: _FakeClamd) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\0")
        await asyncio.sleep(5)

    await clamd.stop()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ClamAVUnavailableError, match="did not answer"):
            await _client(port, timeout_seconds=0.15).scan(b"payload")
    finally:
        server.close()
        await server.wait_closed()


# --- the client-side stream ceiling (R-38(6)) -------------------------------------


async def test_an_oversized_payload_is_refused_without_contacting_the_daemon(
    clamd: _FakeClamd,
) -> None:
    """No INSTREAM command reports the daemon's limit, so the client enforces its own."""
    clamd.handler = _replies(b"stream: OK\0")
    client = _client(clamd.port, max_stream_bytes=1024)

    with pytest.raises(ClamAVPayloadTooLargeError) as caught:
        await client.scan(b"x" * 2048)

    assert caught.value.retryable is False  # a retry cannot shrink the file
    assert clamd.commands == []  # never even connected


def test_settings_refuse_to_boot_when_the_ceiling_is_below_the_upload_limit() -> None:
    """The other half of R-38(6): a misconfigured pair is caught at startup, not at scan."""
    with pytest.raises(ValueError, match="CLAMAV_MAX_STREAM_BYTES"):
        Settings(
            upload=UploadSettings(max_file_bytes=50 * 1024 * 1024),
            clamav=ClamAVSettings(max_stream_bytes=25 * 1024 * 1024),
        )


# --- ping + factory ---------------------------------------------------------------


async def test_ping_accepts_pong(clamd: _FakeClamd) -> None:
    clamd.handler = _replies(b"PONG\0")
    await _client(clamd.port).ping()
    assert clamd.commands == [b"zPING\0"]


async def test_ping_rejects_anything_else(clamd: _FakeClamd) -> None:
    clamd.handler = _replies(b"NOPE\0")
    with pytest.raises(ClamAVProtocolError, match="expected PONG"):
        await _client(clamd.port).ping()


async def test_the_process_wide_client_is_cached_and_closable() -> None:
    first = get_clamav_client()
    assert get_clamav_client() is first
    await close_clamav_client()
    assert get_clamav_client() is not first
    await close_clamav_client()


def test_build_does_not_touch_the_cache() -> None:
    assert build_clamav_client() is not build_clamav_client()
