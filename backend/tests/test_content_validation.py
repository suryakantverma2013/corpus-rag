"""Magic-byte sniffer units (T-202, R-31(3)).

Pure functions — no DB, no app, no fixtures — so these run even when Postgres is down.
The spoofing cases are the point: R-31 made magic-byte detection mandatory precisely
because the declared extension is attacker-controlled.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.security.content_validation import (
    CSV_SUFFIX,
    DOCX_SUFFIX,
    MD_SUFFIX,
    PDF_SUFFIX,
    Family,
    UnsupportedFileTypeError,
    detect_file_type,
    normalized_suffix,
    sniff_family,
)


def _pdf(body: bytes = b"1 0 obj\n<<>>\nendobj\n") -> bytes:
    return b"%PDF-1.7\n" + body + b"%%EOF\n"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": b"<?xml version='1.0'?><Types/>",
            "word/document.xml": b"<?xml version='1.0'?><document><body/></document>",
        }
    )


# ---- suffix parsing ----


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", ".pdf"),
        ("REPORT.PDF", ".pdf"),
        ("archive.tar.gz", ".gz"),
        ("C:\\Users\\x\\notes.MD", ".md"),
        ("/srv/data/table.csv", ".csv"),
        ("noextension", ""),
    ],
)
def test_normalized_suffix(filename: str, expected: str) -> None:
    assert normalized_suffix(filename) == expected


# ---- accepted formats ----


@pytest.mark.parametrize(
    ("payload", "filename", "suffix", "mime"),
    [
        (_pdf(), "report.pdf", PDF_SUFFIX, "application/pdf"),
        (
            _docx(),
            "report.docx",
            DOCX_SUFFIX,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (b"a,b,c\n1,2,3\n", "table.csv", CSV_SUFFIX, "text/csv"),
        (b"# Heading\n\nSome *text*.\n", "notes.md", MD_SUFFIX, "text/markdown"),
    ],
)
def test_accepts_each_format(payload: bytes, filename: str, suffix: str, mime: str) -> None:
    detected = detect_file_type(payload, filename=filename)
    assert (detected.suffix, detected.mime_type) == (suffix, mime)


def test_accepts_utf8_bom_and_crlf() -> None:
    payload = "\ufeffname,city\r\nAda,London\r\n".encode()
    assert detect_file_type(payload, filename="table.csv").suffix == CSV_SUFFIX


def test_accepts_non_ascii_text() -> None:
    payload = "# Notes\n\nrésumé — naïve café ✓\n".encode()
    assert detect_file_type(payload, filename="notes.md").suffix == MD_SUFFIX


# ---- extension spoofing (the R-31 cases) ----


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (b"MZ\x90\x00\x03" + b"\x00" * 64, "report.pdf"),  # PE executable named .pdf
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 64, "notes.md"),  # ELF named .md
        (_pdf(), "notes.md"),  # real PDF named .md
        (_pdf(), "table.csv"),
        (_docx(), "report.pdf"),  # DOCX named .pdf
        (b"a,b\n1,2\n", "report.pdf"),  # text named .pdf
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "report.docx"),  # legacy OLE .doc as .docx
    ],
)
def test_rejects_extension_spoofing(payload: bytes, filename: str) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(payload, filename=filename)


def test_rejects_pdf_magic_at_nonzero_offset() -> None:
    """Leading-junk polyglots must not pass — the magic is required at offset 0."""
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(b"JUNK" + _pdf(), filename="report.pdf")


# ---- container checks ----


def test_rejects_plain_zip_named_docx() -> None:
    with pytest.raises(UnsupportedFileTypeError, match="not a DOCX package"):
        detect_file_type(_zip({"readme.txt": b"hello"}), filename="report.docx")


def test_rejects_ooxml_without_word_document() -> None:
    """An XLSX carries [Content_Types].xml but no word/document.xml."""
    payload = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>"})
    with pytest.raises(UnsupportedFileTypeError, match="not a DOCX package"):
        detect_file_type(payload, filename="report.docx")


def test_rejects_truncated_zip() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(_docx()[:20], filename="report.docx")


# ---- text validation ----


@pytest.mark.parametrize(
    "payload",
    [
        b"name,value\n\x00\x00binary\n",  # NUL byte
        b"col\n\x07bell\n",  # BEL control character
        b"caf\xe9, latin-1 not utf-8\n",  # invalid UTF-8
        "\ufeffname\n".encode("utf-16"),  # UTF-16 is not accepted
    ],
)
def test_rejects_binary_disguised_as_text(payload: bytes) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(payload, filename="table.csv")


@pytest.mark.parametrize(
    "payload",
    [b"#!/bin/sh\necho hi\n", b"<?xml version='1.0'?><x/>", b"<html><body/></html>", b"  <SVG/>"],
)
def test_rejects_active_content_as_markdown(payload: bytes) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(payload, filename="notes.md")


# ---- misc ----


def test_rejects_unaccepted_extension() -> None:
    with pytest.raises(UnsupportedFileTypeError, match="not an accepted format"):
        detect_file_type(b"plain text\n", filename="notes.txt")


def test_rejects_empty_payload() -> None:
    with pytest.raises(UnsupportedFileTypeError, match="empty"):
        detect_file_type(b"", filename="table.csv")


def test_sniff_family_classes() -> None:
    assert sniff_family(b"%PDF-1.4") is Family.PDF
    assert sniff_family(b"PK\x03\x04rest") is Family.ZIP
    assert sniff_family(b"plain text") is Family.TEXT


def test_csv_and_md_are_not_discriminated_by_content() -> None:
    """Documented behaviour: within TEXT the extension alone picks the parser."""
    markdown_bytes = b"# Heading\n\nnot a table\n"
    assert detect_file_type(markdown_bytes, filename="table.csv").suffix == CSV_SUFFIX
    assert detect_file_type(b"a,b\n1,2\n", filename="notes.md").suffix == MD_SUFFIX
