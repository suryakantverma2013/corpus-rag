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

**A table is its own block inside its section (FR-ING-08, R-89, T-223).** It was previously
appended to the section buffer and flushed inside the prose block around it, which left it
marked `text` with no declared header — so the chunker's row-atomicity and repeated-header
rules, both of which were already generic, were simply never reached from this format and a
long table shipped its later chunks as bare `EU | 12 | 15`. A section holding prose, a table
and more prose now yields three blocks on **one** `section` locator, exactly as a PDF page
yields several blocks on one page locator (no fourth `LocatorKind` — R-88(6)). Unlike the PDF
path this needs no detection: a DOCX *declares* its tables, and it declares its header rows
too, in the two ways :func:`_declares_a_header_row` reads.

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
from dataclasses import replace

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
    Extraction,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    Segment,
    clears_table_floor,
    emit_blocks,
    render_row,
    section_locator,
)
from app.ingestion.parsers.text import is_blank, normalize

SUFFIX = ".docx"

#: `Heading 1`..`Heading 9`. Localised Word installs write localised style names, which
#: this misses — such a document still parses, it just lands in one unheaded section.
_HEADING_STYLE = re.compile(r"^heading\s+([1-9])$", re.IGNORECASE)

#: Ratio checks only apply above this expanded size. Small XML parts legitimately
#: compress 50:1 or better, so testing them would reject every real document.
_RATIO_FLOOR_BYTES = 1024 * 1024

#: The two `w:val` spellings that turn a toggle property off. Absent means on.
_HEADER_OFF = frozenset({"0", "false"})


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


def _declares_a_header_row(table: Table) -> bool:
    """Whether row 0 of ``table`` names the columns, from the two signals OOXML carries.

    **`w:trPr/w:tblHeader` on row 0 is the strong one.** The author ticked "Repeat as header
    row", so Word reprints that row at every page break — which is the same claim R-35(11)
    makes about a chunk boundary, arrived at independently by the person who wrote the
    document. Nothing else in a DOCX states it as directly.

    **`w:tblPr/w:tblLook/@w:firstRow` is the weak one, and the weakness is recorded rather
    than hidden.** It enables a table style's first-row formatting, and Word's default table
    insert writes `w:tblLook w:firstRow="1" w:val="04A0"` — measured, and python-docx's own
    default template does the same — so in practice nearly every table asserts it. The
    consequence is a table whose first row is data declaring a header it does not have, which
    costs **one duplicated line on chunks 2..n of a table long enough to split at all**, and
    nothing whatever on a table that fits in one chunk. The alternative is honouring
    `w:tblHeader` alone, which almost no real document sets: that leaves genuine headers
    undeclared on virtually every DOCX, which is precisely the defect T-223 exists to fix. A
    cheap wrong header beats a systematically absent right one.

    **Not inferred from the cells.** `csv.py` guesses because a CSV states nothing; a DOCX
    states something, and second-guessing it here would put an inference in the one place
    `ParsedBlock.header`'s contract says the producer must be authoritative.
    """
    rows = table.rows
    if not rows:
        return False

    row_properties = rows[0]._tr.find(qn("w:trPr"))
    if row_properties is not None:
        flag = row_properties.find(qn("w:tblHeader"))
        if flag is not None:
            # A toggle property: present with no `w:val` means on, and only these two values
            # turn it off (ECMA-376 §17.17.4 — Word writes "0", other producers "false").
            return flag.get(qn("w:val")) not in _HEADER_OFF

    properties = table._tbl.tblPr
    if properties is None:
        return False
    look = properties.find(qn("w:tblLook"))
    return look is not None and look.get(qn("w:firstRow")) == "1"


def _render_table(table: Table, limits: ParserSettings) -> tuple[str, bool, bool]:
    """``(serialised table, is it a table block, does line 0 head it)``.

    Keeping a row on one line preserves the cell adjacency that makes a table row
    meaningful ("Region | Q3 | Q4"); splitting cells into separate lines would scatter
    a row's values across a chunk boundary and make the numbers unattributable.
    Nested tables are not descended into — python-docx exposes a cell's own paragraphs
    only, and nested layout tables are rare enough not to justify the recursion. A
    vertically merged cell repeats its text on every row it spans, because `row.cells`
    reports the grid rather than the merge; that is python-docx's model and predates T-223.

    The second flag is `clears_table_floor` — below it the rendering is unchanged and the
    caller folds it into the surrounding prose, which is exactly the pre-T-223 output.

    The header claim is **positional**, and both guards below exist because
    :func:`emit_blocks` re-reads the header off line 0 of the normalised text. A blank row 0
    is dropped, so the row that would then sit on line 0 is data; and a header with nothing
    under it labels nothing, so declaring it would make the chunker repeat a line that is
    already the block's only content.
    """
    declared = _declares_a_header_row(table)
    lines: list[str] = []
    headed = False
    columns = 0
    for index, row in enumerate(table.rows):
        cells = [cell.text for cell in row.cells]
        if not any(cell.strip() for cell in cells):
            continue
        if not lines:
            columns = len(cells)
            headed = index == 0 and declared
        lines.append(render_row(cells))
    tabular = clears_table_floor(rows=len(lines), columns=columns, limits=limits)
    return "\n".join(lines), tabular, headed and len(lines) > 1


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
    segments: list[Segment] = []
    section_index = 0
    paragraph_ordinal = 0
    section_first_paragraph = 1

    def add_prose(text: str) -> None:
        """Extend the section's open prose run, or start one.

        The blank-line join reproduces the pre-T-223 `"\\n\\n".join(buffer)` exactly, which is
        what makes a table-free document byte-identical. A table between two paragraphs ends
        the run: they were never adjacent, and running them together invents a sentence
        boundary the chunker would then embed — `compose_page`'s reason, one format over.
        """
        if segments and segments[-1].extraction is Extraction.TEXT:
            segments[-1] = replace(segments[-1], text=f"{segments[-1].text}\n\n{text}")
        else:
            segments.append(Segment(text=text))

    def flush(path: tuple[str, ...], first_paragraph: int) -> None:
        """Emit this section's segments as an ordered run of blocks on **one** locator.

        One `section_index` per section, not per block: a section holding prose, a table and
        more prose is one section that yields three blocks, exactly as a PDF page yields
        several blocks on one page locator (R-88(6)). Numbering per block would make
        `section_index` a block ordinal wearing a section's name, and every citation into a
        table-bearing document would then name a section the document does not have.

        Normalised here rather than only inside `emit_blocks` so the emptiness test is the one
        the emitter will apply — a section of nothing but control bytes must not consume a
        `section_index` and then emit no blocks. `normalize` is idempotent, so the emitter's
        own call is a no-op and the emitted text is unchanged.
        """
        nonlocal segments, section_index
        pending = [replace(segment, text=normalize(segment.text)) for segment in segments]
        segments = []
        pending = [segment for segment in pending if not is_blank(segment.text)]
        if not pending:
            return
        section_index += 1
        locator = section_locator(
            path,
            section_index,
            fallback_label=f"paragraph {first_paragraph}",  # TBD(§8.4)
        )
        for segment in pending:
            emit_blocks(
                blocks,
                segment.text,
                locator=locator,
                budget=budget,
                limits=limits,
                extraction=segment.extraction,
                headed=segment.headed,
            )

    try:
        for item in _iter_body(document):
            if isinstance(item, Table):
                rendered, tabular, headed = _render_table(item, limits)
                if not rendered:
                    continue
                if tabular:
                    # Its own segment, always: `extraction: "table"` has to be true of the
                    # whole block, and the chunker's row-atomicity rule keys off it — a block
                    # that were half prose would forbid word-level splits in the prose half.
                    segments.append(
                        Segment(text=rendered, extraction=Extraction.TABLE, headed=headed)
                    )
                else:
                    add_prose(rendered)
                continue

            # Deliberately not incremented for a table: the fallback label reads
            # "paragraph N", so counting a table would name something the reader cannot find.
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
                add_prose(text.strip())
                continue

            if not is_blank(text):
                # A section opening with a table leaves `segments` non-empty, so the start
                # ordinal is not reset for the paragraph after it. `section_locator` consults
                # `fallback_label` only when the heading path is empty, and with no headings
                # there is exactly one section whose first paragraph is #1 by construction —
                # so the only reachable wrong output is "paragraph 1" on a headingless
                # document that opens with a table. Display copy, already `# TBD(§8.4)`, and
                # "paragraph 0" would be worse.
                if not segments:
                    section_first_paragraph = paragraph_ordinal
                add_prose(text)
    except Exception as exc:
        if isinstance(exc, DocumentTooComplexError):
            raise
        raise CorruptDocumentError(f"DOCX body could not be read: {exc}") from exc

    flush(heading_path, section_first_paragraph)

    if not blocks:
        raise NoExtractableTextError("this DOCX contains no readable text")

    # `page_count` stays None — see the module docstring.
    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks))
