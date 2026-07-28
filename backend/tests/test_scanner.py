"""Structural + signature screening at the head of the worker (T-207, R-31/R-32).

Fixtures are generated in-process by the same libraries that read them — a real DOCX from
`python-docx` repacked with a `vbaProject.bin` member, real PDFs from PyMuPDF with active
content written into the catalog — so no binaries live in the repo and the fixtures cannot
drift from what the screen actually parses.

Two of these tests exist to stop *false* positives rather than catch true ones
(`test_a_navigation_open_action_is_not_a_finding`, `test_a_corrupt_container_is_left_to_the
_parser`). Both guard against failure modes that would be far more common in production
than real malware: an `/OpenAction` that merely opens the document at page 1 is written by
most PDF producers, and relabelling every corrupt upload "malware detected" would be both
wrong and unactionable.
"""

from __future__ import annotations

import io
import zipfile

import docx
import pymupdf
import pytest

from app.config import ClamAVSettings, ScannerSettings, Settings
from app.ingestion.scanner import (
    REASON_ACTIVE_CONTENT,
    REASON_MACRO,
    REASON_MALWARE,
    MalwareScanner,
    ScanVerdict,
    StructuralOnlyScanner,
    build_scanner,
)
from app.services.clamav import ClamAVScanReport, ClamAVUnavailableError

# --- fixtures built in-process ------------------------------------------------------


def _docx_bytes(*, macro: bool = False) -> bytes:
    document = docx.Document()
    document.add_paragraph("Quarterly figures for the northern region.")
    raw = io.BytesIO()
    document.save(raw)
    if not macro:
        return raw.getvalue()

    # Repack with the macro payload. A DOCM renamed to .docx is byte-for-byte a ZIP, so
    # T-202's magic-byte sniffing cannot tell the two apart — which is exactly why R-32
    # puts this check here.
    out = io.BytesIO()
    raw.seek(0)
    with zipfile.ZipFile(raw) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0" + b"ole2 macro storage")
    return out.getvalue()


def _pdf_bytes(*, catalog_keys: dict[str, str] | None = None, attach: bool = False) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 720), "Corpus answers strictly from the documents you give it.")
    for key, value in (catalog_keys or {}).items():
        document.xref_set_key(document.pdf_catalog(), key, value)
    if attach:
        document.embfile_add("payload.bin", b"attached bytes")
    raw = document.tobytes()
    document.close()
    return raw


class _StubClamAV:
    """Stands in for `ClamAVClient` — records calls, returns or raises what it is told."""

    def __init__(self, report: ClamAVScanReport | None = None, error: Exception | None = None):
        self.report = report or ClamAVScanReport(infected=False)
        self.error = error
        self.calls = 0

    async def scan(self, payload: bytes) -> ClamAVScanReport:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.report


# --- DOCX: disguised macros ---------------------------------------------------------


async def test_a_docm_wearing_a_docx_extension_is_detected() -> None:
    result = await StructuralOnlyScanner().scan(_docx_bytes(macro=True), filename="budget.docx")
    assert result.verdict is ScanVerdict.INFECTED
    assert result.reason_code == REASON_MACRO
    assert "vbaProject.bin" in (result.detail or "")


async def test_an_ordinary_docx_passes() -> None:
    result = await StructuralOnlyScanner().scan(_docx_bytes(), filename="budget.docx")
    assert result.verdict is ScanVerdict.CLEAN


async def test_a_corrupt_container_is_left_to_the_parser() -> None:
    """Not a detection: the parser reports CORRUPT_DOCUMENT with an actionable message."""
    result = await StructuralOnlyScanner().scan(b"PK\x03\x04 truncated", filename="x.docx")
    assert result.verdict is ScanVerdict.CLEAN


# --- PDF: active content ------------------------------------------------------------


async def test_document_level_javascript_is_detected() -> None:
    payload = _pdf_bytes(catalog_keys={"Names": "<</JavaScript<</Names[(a)<</S/JavaScript>>]>>>>"})
    result = await StructuralOnlyScanner().scan(payload, filename="report.pdf")
    assert result.verdict is ScanVerdict.INFECTED
    assert result.reason_code == REASON_ACTIVE_CONTENT
    assert "/JavaScript" in (result.detail or "")


async def test_an_open_action_that_runs_javascript_is_detected() -> None:
    payload = _pdf_bytes(catalog_keys={"OpenAction": "<</S/JavaScript/JS(app.alert\\(1\\))>>"})
    result = await StructuralOnlyScanner().scan(payload, filename="report.pdf")
    assert result.verdict is ScanVerdict.INFECTED
    assert "OpenAction" in (result.detail or "")


async def test_a_navigation_open_action_is_not_a_finding() -> None:
    """`/OpenAction` is only interesting when it *runs* something.

    "Open at page 1, fit width" is written by most PDF producers. Flagging it would reject
    a large share of ordinary documents while catching nothing — a false-positive rate that
    would make the whole screen untrustworthy.
    """
    payload = _pdf_bytes(catalog_keys={"OpenAction": "<</S/GoTo/D[0 0 R/XYZ 0 792 0]>>"})
    result = await StructuralOnlyScanner().scan(payload, filename="report.pdf")
    assert result.verdict is ScanVerdict.CLEAN


async def test_an_embedded_file_is_detected() -> None:
    result = await StructuralOnlyScanner().scan(_pdf_bytes(attach=True), filename="r.pdf")
    assert result.verdict is ScanVerdict.INFECTED
    assert "EmbeddedFile" in (result.detail or "")


async def test_an_ordinary_pdf_passes() -> None:
    result = await StructuralOnlyScanner().scan(_pdf_bytes(), filename="report.pdf")
    assert result.verdict is ScanVerdict.CLEAN


async def test_an_unreadable_pdf_is_left_to_the_parser() -> None:
    result = await StructuralOnlyScanner().scan(b"%PDF-1.7\ngarbage", filename="r.pdf")
    assert result.verdict is ScanVerdict.CLEAN


@pytest.mark.parametrize("filename", ["notes.md", "rows.csv"])
async def test_text_formats_have_no_container_to_screen(filename: str) -> None:
    result = await StructuralOnlyScanner().scan(b"alpha,beta\n1,2\n", filename=filename)
    assert result.verdict is ScanVerdict.CLEAN


# --- the combined scanner -----------------------------------------------------------


async def test_a_signature_hit_is_reported_with_its_name() -> None:
    stub = _StubClamAV(ClamAVScanReport(infected=True, signature="Eicar-Test-Signature"))
    result = await MalwareScanner(stub).scan(_pdf_bytes(), filename="report.pdf")
    assert result.verdict is ScanVerdict.INFECTED
    assert result.reason_code == REASON_MALWARE
    assert result.signature == "Eicar-Test-Signature"


async def test_a_clean_document_passes_both_screens() -> None:
    stub = _StubClamAV()
    result = await MalwareScanner(stub).scan(_pdf_bytes(), filename="report.pdf")
    assert result.verdict is ScanVerdict.CLEAN
    assert stub.calls == 1


async def test_a_structural_hit_skips_the_network_round_trip() -> None:
    stub = _StubClamAV()
    result = await MalwareScanner(stub).scan(_docx_bytes(macro=True), filename="b.docx")
    assert result.reason_code == REASON_MACRO
    assert stub.calls == 0


async def test_an_unreachable_daemon_propagates_rather_than_passing_the_file() -> None:
    """R-32 fails closed: the scanner raises, it never returns CLEAN it could not verify."""
    stub = _StubClamAV(error=ClamAVUnavailableError("connection refused"))
    with pytest.raises(ClamAVUnavailableError):
        await MalwareScanner(stub).scan(_pdf_bytes(), filename="report.pdf")


# --- backend selection --------------------------------------------------------------


def test_the_structural_backend_still_screens() -> None:
    """R-38(7): there is deliberately no setting that disables screening outright."""
    scanner = build_scanner(Settings(scanner=ScannerSettings(backend="structural")))
    assert isinstance(scanner, StructuralOnlyScanner)


def test_the_clamav_backend_is_the_default() -> None:
    settings = Settings(clamav=ClamAVSettings(host="127.0.0.1"))
    assert isinstance(build_scanner(settings), MalwareScanner)
