"""The in-tree OCR sidecar client (T-217, R-88 §8.78).

Tested against a **real HTTP server** — a throwaway `ThreadingHTTPServer` standing in for the
sidecar — rather than a mocked transport, on `test_clamav.py`'s reasoning: the risk in this
module is the wire contract, and a mocked transport agrees with whatever contract the client
invented.

Two tests carry most of the weight.
:func:`test_a_malformed_body_is_never_an_empty_page` is the fail-open trap this client exists
to close: "recognised nothing" is a legitimate outcome for a blank page, so if a broken
sidecar also produced an empty result the two would be indistinguishable and a whole corpus
of scans would ingest as blank. And
:func:`test_an_oversized_raster_is_refused_before_the_request_is_made` asserts the ceiling by
the **absence** of a request, so a check moved after the POST fails rather than merely
wasting transfer (R-63(6)'s shape).

:func:`test_the_same_page_recognises_identically_twice` is the one R-88(1) rests on and the
one a fake cannot make: it needs the real engine. It is gated on ``OCR_LIVE_TEST`` and the
running sidecar, on the ``KEYCLOAK_LIVE_ADMIN_PASSWORD`` convention.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from app.config import OcrSettings, ParserSettings, Settings
from app.services.ocr import (
    LANGUAGE_HEADER,
    OcrClient,
    OcrImageTooLargeError,
    OcrPageResult,
    OcrProtocolError,
    OcrUnavailableError,
    OcrWord,
    build_ocr_client,
    close_ocr_client,
    get_ocr_client,
)

PNG = b"\x89PNG\r\n\x1a\n-not-really-a-png-"


@dataclass
class _Received:
    method: str
    path: str
    body: bytes
    headers: dict[str, str]


@dataclass
class _FakeSidecar:
    """Serves whatever `responder` returns and records what it was asked."""

    received: list[_Received] = field(default_factory=list)
    responder: Callable[[_Received], tuple[int, bytes]] | None = None
    port: int = 0
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    def start(self) -> None:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                record = _Received(
                    method=self.command,
                    path=self.path,
                    body=self.rfile.read(length) if length else b"",
                    headers={key.lower(): value for key, value in self.headers.items()},
                )
                outer.received.append(record)
                assert outer.responder is not None, "the test must set a responder"
                status, payload = outer.responder(record)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle  # noqa: N815 - BaseHTTPRequestHandler's contract
            do_POST = _handle  # noqa: N815 - BaseHTTPRequestHandler's contract

            def log_message(self, fmt: str, *args: object) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def sidecar() -> Iterator[_FakeSidecar]:
    server = _FakeSidecar()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _client(port: int, **overrides: object) -> OcrClient:
    ocr = OcrSettings(host="127.0.0.1", port=port, **overrides)
    return OcrClient(Settings(ocr=ocr))


def _body(**payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _recognized(
    text: str, words: list[dict[str, object]]
) -> Callable[[_Received], tuple[int, bytes]]:
    return lambda _record: (
        200,
        _body(text=text, words=words, engine={"tesseract": "5.5.0", "langs": ["eng", "osd"]}),
    )


# --- the happy path ---------------------------------------------------------------------


def test_a_recognised_page_carries_its_text_and_per_word_confidences(
    sidecar: _FakeSidecar,
) -> None:
    sidecar.responder = _recognized(
        "Invoice 4218\n",
        [{"text": "Invoice", "conf": 96.5}, {"text": "4218", "conf": 91.5}],
    )

    result = _client(sidecar.port).recognize(PNG)

    assert result.text == "Invoice 4218\n"
    assert result.words == (OcrWord("Invoice", 96.5), OcrWord("4218", 91.5))
    assert result.engine_version == "5.5.0"
    assert result.languages == ("eng", "osd")
    assert result.mean_confidence == pytest.approx(94.0)


def test_the_raster_is_sent_as_the_body_under_the_configured_language(
    sidecar: _FakeSidecar,
) -> None:
    sidecar.responder = _recognized("x", [])

    _client(sidecar.port, languages="eng+deu").recognize(PNG)

    (request,) = sidecar.received
    assert request.method == "POST"
    assert request.path == "/recognize"
    assert request.body == PNG
    assert request.headers["content-type"] == "image/png"
    assert request.headers[LANGUAGE_HEADER.lower()] == "eng+deu"


def test_a_malformed_language_never_becomes_a_request() -> None:
    """The language reaches the sidecar's ``argv``, so it is refused at the boundary.

    The client has no per-call override for exactly this reason: with the value taken only
    from validated settings, "this client cannot send a malformed language" is a property of
    the code rather than a discipline the next caller has to remember. The sidecar validates
    it a second time, which is defence in depth and not a substitute for this.
    """
    with pytest.raises(ValidationError):
        OcrSettings(languages="eng; rm -rf /")
    with pytest.raises(ValidationError):
        OcrSettings(languages="")

    assert OcrSettings(languages="eng+deu").languages == "eng+deu"


# --- the R-88(8) aggregation rule -------------------------------------------------------


def test_the_mean_confidence_ignores_unscored_and_blank_words() -> None:
    """Tesseract writes ``conf = -1`` on rows that are not words.

    Counting those as zero would drag a healthy page's mean towards the floor R-88(8)
    discards on, so a page could be thrown away for having structure.
    """
    page = OcrPageResult(
        text="Invoice 4218",
        words=(
            OcrWord("Invoice", 90.0),
            OcrWord("", -1.0),
            OcrWord("4218", 80.0),
            OcrWord("   ", 5.0),
        ),
        engine_version="5.5.0",
        languages=("eng",),
    )

    assert page.mean_confidence == pytest.approx(85.0)


def test_a_page_with_nothing_recognised_reports_no_confidence_rather_than_zero() -> None:
    """``0.0`` is a real score meaning "recognised, and garbage"; ``None`` is "nothing here".

    A blank page and a garbled one are different facts, and a caller comparing against the
    R-88(8) floor must be made to decide about the difference rather than have `0.0` decide
    it silently.
    """
    blank = OcrPageResult(text="\n", words=(), engine_version="5.5.0", languages=("eng",))

    assert blank.mean_confidence is None


def test_a_recognised_page_reports_the_mean_the_sidecar_did_not_compute(
    sidecar: _FakeSidecar,
) -> None:
    """The aggregation is the client's, not the container's — it is R-88(8)'s whole input."""
    sidecar.responder = _recognized(
        "a b",
        [{"text": "a", "conf": 100.0}, {"text": "b", "conf": 50.0}, {"text": "", "conf": -1}],
    )

    assert _client(sidecar.port).recognize(PNG).mean_confidence == pytest.approx(75.0)


# --- failure directions -----------------------------------------------------------------


def test_an_unreachable_sidecar_raises_unavailable(sidecar: _FakeSidecar) -> None:
    port = sidecar.port
    sidecar.stop()

    with pytest.raises(OcrUnavailableError, match="unreachable"):
        _client(port).recognize(PNG)


def test_a_sidecar_that_does_not_answer_in_time_raises_unavailable(
    sidecar: _FakeSidecar,
) -> None:
    def _slow(_record: _Received) -> tuple[int, bytes]:
        time.sleep(1.0)
        return 200, _body(text="", words=[], engine={})

    sidecar.responder = _slow

    with pytest.raises(OcrUnavailableError, match="did not answer within"):
        _client(sidecar.port, timeout_seconds=0.1).recognize(PNG)


def test_a_sidecar_error_status_is_never_a_result(sidecar: _FakeSidecar) -> None:
    sidecar.responder = lambda _record: (500, _body(error="tesseract exited 1"))

    with pytest.raises(OcrProtocolError, match="500"):
        _client(sidecar.port).recognize(PNG)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{}", id="empty-object"),
        pytest.param(b'{"text": "x"}', id="no-words"),
        pytest.param(b'{"text": "x", "words": "nope", "engine": {}}', id="words-not-a-list"),
        pytest.param(b'{"words": [], "engine": {}}', id="no-text"),
        pytest.param(b'{"text": "x", "words": [{"text": "a"}], "engine": {}}', id="word-no-conf"),
        pytest.param(b'{"text": "x", "words": ["a"], "engine": {}}', id="word-not-an-object"),
        pytest.param(b"[1, 2]", id="not-an-object"),
        pytest.param(b"<html>gateway</html>", id="not-json"),
    ],
)
def test_a_malformed_body_is_never_an_empty_page(sidecar: _FakeSidecar, payload: bytes) -> None:
    """A broken sidecar must not be indistinguishable from a corpus of blank pages.

    This is R-32's clamd trap in a second place: there, an oversized stream was truncated
    and its prefix reported *clean*; here, a response the client cannot read would be
    reported as a page with no text — and recognition fails open, so nothing downstream
    would ever notice.
    """
    sidecar.responder = lambda _record: (200, payload)

    with pytest.raises(OcrProtocolError):
        _client(sidecar.port).recognize(PNG)


def test_an_oversized_raster_is_refused_before_the_request_is_made(
    sidecar: _FakeSidecar,
) -> None:
    """Asserted by the absence of a request, not by the exception.

    The exception passes just as well with the check moved after the POST, which is the
    version that ships the bytes anyway.
    """
    sidecar.responder = _recognized("unused", [])

    with pytest.raises(OcrImageTooLargeError, match="OCR_MAX_IMAGE_BYTES"):
        _client(sidecar.port, max_image_bytes=1024).recognize(b"\x00" * 2048)

    assert sidecar.received == []


# --- the readiness probe's half ---------------------------------------------------------


def test_ping_accepts_a_healthy_sidecar(sidecar: _FakeSidecar) -> None:
    sidecar.responder = lambda _record: (200, _body(status="ok", tesseract="5.5.0"))

    _client(sidecar.port).ping()

    assert sidecar.received[0].path == "/health"


def test_ping_refuses_a_sidecar_that_does_not_report_ok(sidecar: _FakeSidecar) -> None:
    sidecar.responder = lambda _record: (200, _body(status="degraded"))

    with pytest.raises(OcrProtocolError, match="status=ok"):
        _client(sidecar.port).ping()


# --- the seam ---------------------------------------------------------------------------


async def test_the_process_wide_client_is_cached_and_released() -> None:
    try:
        first = get_ocr_client()
        assert get_ocr_client() is first
    finally:
        await close_ocr_client()

    assert get_ocr_client() is not first
    await close_ocr_client()


def test_build_does_not_touch_the_cache() -> None:
    assert build_ocr_client() is not build_ocr_client()


def test_the_feature_ships_off() -> None:
    """R-88(12). The off state is what makes a missing sidecar the pre-Rev-0.55 behaviour."""
    assert ParserSettings().ocr_enabled is False


# --- NFR-SEC-09: isolation, asserted as the absence of an alternative -------------------

#: The in-process bindings a future contributor would reach for, each of which would put the
#: engine and its image decoders back inside the worker - which is the one thing NFR-SEC-09
#: forbids. Named rather than pattern-matched, so adding one is a decision someone writes down.
_IN_PROCESS_ENGINES = ("pytesseract", "tesserocr", "easyocr", "paddleocr", "rapidocr")


def test_recognition_has_no_in_process_engine() -> None:
    """NFR-SEC-09's first clause. An isolation claim can only be asserted as an absence.

    The requirement is not "we call a sidecar" — it is that there is nothing else to call.
    A `pytesseract` in the dependency set would satisfy every other test in this file while
    putting the decoders back in the process holding the worker's database session and its
    object-storage credentials.
    """
    pyproject = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    declared = [engine for engine in _IN_PROCESS_ENGINES if engine in pyproject]
    assert not declared, f"in-process OCR engines declared: {declared}"

    source = pathlib.Path(inspect.getfile(OcrClient)).read_text(encoding="utf-8")
    assert "subprocess" not in source, "the client must not shell out; the sidecar runs the engine"
    assert "ctypes" not in source, "the client must not load the engine in-process"


def test_the_only_destination_is_the_configured_sidecar() -> None:
    """NFR-SEC-09's second clause, and R-86(3)'s egress prohibition one requirement along.

    Every byte this module sends goes to an address composed from settings, so a deployment
    that points `OCR_HOST` at its own container cannot be talked out of it by anything in the
    code. There is no vendor endpoint to fall back to — R-88(1) refused the hosted-recogniser
    class outright, so there is nothing here to make optional.
    """
    client = OcrClient(Settings(ocr=OcrSettings(host="ocr.internal", port=9999)))

    assert client.endpoint == "http://ocr.internal:9999"


# --- live: the property the whole design rests on ---------------------------------------


@pytest.mark.skipif(
    os.environ.get("OCR_LIVE_TEST", "") == "",
    reason="OCR_LIVE_TEST is unset; live sidecar tests skipped",
)
def test_the_same_page_recognises_identically_twice() -> None:
    """R-88(1): recognition must be byte-reproducible, or vector reuse stops working.

    `plan_chunk_set` reuses a vector by set membership on `embedding_fingerprint`, which
    folds in `chunk_text`. Text that differs between two passes over one page therefore
    leaves `reused` permanently empty, and every `/replace`, FR-ING-04 retry and T-608
    rebuild silently re-embeds the whole document at full cost. Nothing fails; only a bill
    would show it.

    No fake can assert this — it is a property of the engine, its language pack and the
    flags the sidecar pins. Run the sidecar first:

        docker compose -f deployment/docker-compose.yml --profile ocr up -d --build
    """
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 120), "Quarterly revenue rose to 4,218 units.", fontsize=18)
    page.insert_text((72, 160), "Invoice number INV-2026-0817 was settled.", fontsize=18)
    raster = pymupdf.open(stream=document.tobytes(), filetype="pdf")[0].get_pixmap(dpi=300)
    image = raster.tobytes("png")

    client = OcrClient(Settings(ocr=OcrSettings()))
    first = client.recognize(image)
    second = client.recognize(image)
    client.close()

    assert first.text == second.text
    assert first.words == second.words
    # Not merely stable but correct: a client that returned "" twice would satisfy the
    # assertions above.
    assert "4,218" in first.text
    assert first.mean_confidence is not None
    assert first.mean_confidence > 80


@pytest.mark.skipif(
    os.environ.get("OCR_LIVE_TEST", "") == "",
    reason="OCR_LIVE_TEST is unset; live sidecar tests skipped",
)
def test_the_live_sidecar_reports_the_pinned_engine_and_language() -> None:
    """The pins are the determinism argument, so they are asserted rather than trusted.

    The language pack is a named volume, and Docker seeds one from the image only while it
    is empty — so an engine or pack that has drifted from `deployment/ocr/Dockerfile` is
    exactly the state that breaks R-88(1) without breaking anything visible.
    """
    client = OcrClient(Settings(ocr=OcrSettings()))
    try:
        client.ping()
        result = client.recognize(_one_pixel_png())
    finally:
        client.close()

    assert result.engine_version.startswith("5.5.")
    assert "eng" in result.languages


@pytest.mark.skipif(
    os.environ.get("OCR_LIVE_TEST", "") == "",
    reason="OCR_LIVE_TEST is unset; live sidecar tests skipped",
)
def test_an_engine_failure_does_not_carry_the_page_back() -> None:
    """A failed recognition must not put document bytes in the backend's logs.

    Tesseract falls back to reading an unrecognised file as a *list of image paths*, so its
    stderr quotes the input back — and the input is a page raster. The sidecar therefore
    returns the exit code and keeps the engine's words inside the container, where the trust
    boundary already is. Found by sending non-image bytes at the running sidecar; no unit
    test can reach it, because the fake is the thing that would have to leak.
    """
    marker = b"CONFIDENTIAL-PAGE-CONTENT-MARKER"

    client = OcrClient(Settings(ocr=OcrSettings()))
    try:
        with pytest.raises(OcrProtocolError) as raised:
            client.recognize(marker)
    finally:
        client.close()

    assert "500" in str(raised.value)
    assert marker.decode() not in str(raised.value)


def _one_pixel_png() -> bytes:
    import pymupdf

    document = pymupdf.open()
    document.new_page(width=8, height=8)
    return pymupdf.open(stream=document.tobytes(), filetype="pdf")[0].get_pixmap().tobytes("png")
