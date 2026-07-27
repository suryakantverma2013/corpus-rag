"""DOCX parsing via python-docx (T-203, §10.1, FR-KBM-05).

Two things make this more than "read the paragraphs".

**The zip pre-flight is the R-31(3) control.** A DOCX is a ZIP, and a zip bomb is a
structurally valid OOXML package — the upload sniffer cannot tell one from a real
document, because both have `[Content_Types].xml` and `word/document.xml` right where
they should be. So before python-docx decompresses anything, :func:`_check_zip_limits`
reads the central directory (which decompresses nothing) and rejects implausible member
counts, expanded sizes and compression ratios.

**Body order, not paragraph order.** `document.paragraphs` silently omits every table,
so a document whose data lives in tables would ingest as a handful of headings and
nothing else — the failure would look like a bad retrieval result months later, not a
parse error. Walking `document.element.body` visits paragraphs and tables interleaved in
true document order.

There are no page numbers here, and none are invented (R-34). DOCX stores no pagination —
page breaks are computed by the renderer from font metrics and page size, so any "page 7"
this module produced would be fiction that a citation then points at. The locator is the
heading path, which *is* stored and is stable across re-saves.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator

# Absolute imports: `docx` here is python-docx, not this module (PEP 328).
from docx import Document as open_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    DocumentTooComplexError,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    section_locator,
    split_text,
)
from app.ingestion.parsers.text import is_blank, normalize

SUFFIX = ".docx"

#: `Heading 1`..`Heading 9`. Localised Word installs write localised style names, which
#: this misses — such a document still parses, it just lands in one unheaded section.
_HEADING_STYLE = re.compile(r"^heading\s+([1-9])$", re.IGNORECASE)

#: Ratio checks only apply above this expanded size. Small XML parts legitimately
#: compress 50:1 or better, so testing them would reject every real document.
_RATIO_FLOOR_BYTES = 1024 * 1024


def _check_zip_limits(payload: bytes, limits: ParserSettings) -> None:
    """Reject decompression bombs from the central directory alone (R-31(3))."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise CorruptDocumentError(f"DOCX is not a readable ZIP container: {exc}") from exc

    if len(infos) > limits.docx_max_members:
        raise DocumentTooComplexError(
            f"DOCX contains {len(infos):,} parts — the limit is {limits.docx_max_members:,}"
        )

    total_expanded = sum(info.file_size for info in infos)
    if total_expanded > limits.docx_max_expanded_bytes:
        raise DocumentTooComplexError(
            f"DOCX expands to {total_expanded:,} bytes — the limit is "
            f"{limits.docx_max_expanded_bytes:,}"
        )

    total_compressed = sum(info.compress_size for info in infos)
    if total_expanded > _RATIO_FLOOR_BYTES and total_compressed > 0:
        ratio = total_expanded / total_compressed
        if ratio > limits.docx_max_compression_ratio:
            raise DocumentTooComplexError(
                f"DOCX compression ratio {ratio:,.0f}:1 exceeds the "
                f"{limits.docx_max_compression_ratio:,.0f}:1 limit"
            )

    # A single hostile member can hide inside an otherwise ordinary package, so the
    # aggregate check above is not sufficient on its own.
    for info in infos:
        if info.file_size > _RATIO_FLOOR_BYTES and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > limits.docx_max_compression_ratio:
                raise DocumentTooComplexError(
                    f"DOCX part {info.filename!r} has compression ratio {ratio:,.0f}:1, "
                    f"above the {limits.docx_max_compression_ratio:,.0f}:1 limit"
                )


def _render_table(table: Table) -> str:
    """Flatten a table to one line per row, cells pipe-separated.

    Keeping a row on one line preserves the cell adjacency that makes a table row
    meaningful ("Region | Q3 | Q4"); splitting cells into separate lines would scatter
    a row's values across a chunk boundary and make the numbers unattributable.
    Nested tables are not descended into — python-docx exposes a cell's own paragraphs
    only, and nested layout tables are rare enough not to justify the recursion.
    """
    lines = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _iter_body(document) -> Iterator[Paragraph | Table]:  # noqa: ANN001 — python-docx Document
    """Yield paragraphs and tables in true document order."""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _heading_level(paragraph: Paragraph) -> int | None:
    """Heading depth 1..9, or ``None`` for body text."""
    style = paragraph.style
    name = getattr(style, "name", None)
    if not name:
        return None
    match = _HEADING_STYLE.match(name)
    return int(match.group(1)) if match else None


def parse(payload: bytes, *, limits: ParserSettings) -> ParsedDocument:
    """Extract heading-scoped text from a DOCX."""
    _check_zip_limits(payload, limits)

    try:
        document = open_docx(io.BytesIO(payload))
    except Exception as exc:
        raise CorruptDocumentError(f"DOCX could not be opened: {exc}") from exc

    budget = CharBudget(limits.max_extracted_chars)
    blocks: list[ParsedBlock] = []
    heading_path: tuple[str, ...] = ()
    buffer: list[str] = []
    section_index = 0
    paragraph_ordinal = 0
    section_first_paragraph = 1

    def flush(path: tuple[str, ...], first_paragraph: int) -> None:
        nonlocal buffer, section_index
        content = normalize("\n\n".join(buffer))
        buffer = []
        if is_blank(content):
            return
        budget.add(content)
        section_index += 1
        locator = section_locator(
            path,
            section_index,
            fallback_label=f"paragraph {first_paragraph}",  # TBD(§8.4)
        )
        for part in split_text(content, limits.max_block_chars):
            blocks.append(ParsedBlock(text=part, locator=locator, order=len(blocks)))

    try:
        for item in _iter_body(document):
            if isinstance(item, Table):
                rendered = _render_table(item)
                if rendered:
                    buffer.append(rendered)
                continue

            paragraph_ordinal += 1
            text = item.text
            level = _heading_level(item)
            if level is not None and not is_blank(text):
                flush(heading_path, section_first_paragraph)
                # Truncate to the parent depth, then descend. A document that jumps
                # H1 → H3 yields a two-element path rather than a padded one.
                heading_path = heading_path[: level - 1] + (text.strip(),)
                section_first_paragraph = paragraph_ordinal
                # The heading leads its own section so a chunk carries its context.
                buffer.append(text.strip())
                continue

            if not is_blank(text):
                if not buffer:
                    section_first_paragraph = paragraph_ordinal
                buffer.append(text)
    except Exception as exc:
        if isinstance(exc, DocumentTooComplexError):
            raise
        raise CorruptDocumentError(f"DOCX body could not be read: {exc}") from exc

    flush(heading_path, section_first_paragraph)

    if not blocks:
        raise NoExtractableTextError("this DOCX contains no readable text")

    # `page_count` stays None — see the module docstring.
    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks))
