"""CSV parsing via the standard library (T-203, §10.1 as amended by R-34, FR-KBM-05).

§10.1 originally named pandas. It was replaced here because the job is "read text and
remember which rows it came from", and pandas actively works against both halves: it
materialises a 50 MB upload as an in-memory frame, and its type inference rewrites the
text before we ever see it — `007` becomes `7`, `NA` becomes a null, a long digit string
becomes a float in scientific notation. Every one of those is a silent corruption of a
document a user will later ask questions about. `csv.reader` streams, and returns exactly
the characters in the file.

**Rows are counted as records, not lines.** A quoted field containing a newline spans
several physical lines but is one record, and the `csv` module already knows the
difference. Counting lines would drift the locator on exactly the files where an accurate
citation matters most.

**The header rides along with every block.** A chunk reading `Acme | EU | 12` is
meaningless on its own; with `company | region | revenue` above it, the embedding and the
answer both have something to work with. The cost is one duplicated line per block.
"""

from __future__ import annotations

# Absolute import: stdlib `csv`, not this module (PEP 328).
import csv
import io

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    DocumentTooComplexError,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    render_row,
    rows_locator,
    split_text,
)
from app.ingestion.parsers.text import normalize

SUFFIX = ".csv"

#: Bytes of the payload handed to the dialect sniffer.
_SNIFF_SAMPLE_BYTES = 16384

#: Delimiters worth guessing between. Restricting the set stops the sniffer picking some
#: letter that happens to recur — its unconstrained mode does exactly that on prose-heavy
#: columns.
_DELIMITERS = ",;\t|"


def _decode(payload: bytes) -> str:
    """UTF-8 (BOM-tolerant) or fail.

    T-202 already validated this at upload, but the worker re-checks rather than trusting
    a state it cannot see: the object may have been written by a different code path
    (T-209 replace), and decoding is cheap next to the parse that follows.
    """
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorruptDocumentError(f"CSV is not valid UTF-8: {exc}") from exc


def _sniff_dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    """Guess the delimiter, falling back to plain comma."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
    except csv.Error:
        return csv.excel  # a file the sniffer was too unsure about


def _is_numeric(value: str) -> bool:
    try:
        float(value.replace(",", "").replace(" ", ""))
    except ValueError:
        return False
    return True


def _looks_like_header(cells: list[str]) -> bool:
    """Decide whether row 1 names the columns.

    `csv.Sniffer.has_header` is deliberately not used: it infers from *column type*
    consistency, so an all-text CSV — the common case for a document corpus — routinely
    comes back False, and the consequence is not cosmetic. A missed header shifts every
    row number by one, so every citation this document ever produces points at the wrong
    line. This test asks the question that actually matters: do these cells look like
    column *names*?

    A single-column CSV of prose is the known false positive; it costs one lost row.
    """
    values = [cell.strip() for cell in cells]
    if not values or any(not value for value in values):
        return False
    lowered = [value.casefold() for value in values]
    if len(set(lowered)) != len(lowered):  # repeated labels are data, not a header
        return False
    return not any(_is_numeric(value) for value in values)


def parse(payload: bytes, *, limits: ParserSettings) -> ParsedDocument:
    """Extract row-grouped text from a CSV."""
    text = _decode(payload)
    dialect = _sniff_dialect(text[:_SNIFF_SAMPLE_BYTES])

    budget = CharBudget(limits.max_extracted_chars)
    blocks: list[ParsedBlock] = []
    header_line = ""
    pending: list[str] = []
    block_start = 1
    record_number = 0

    def flush(end: int) -> None:
        nonlocal pending
        if not pending:
            return
        lines = [header_line, *pending] if header_line else list(pending)
        pending = []
        content = normalize("\n".join(lines))
        if not content:
            return
        budget.add(content)
        locator = rows_locator(block_start, end)
        for part in split_text(content, limits.max_block_chars):
            blocks.append(ParsedBlock(text=part, locator=locator, order=len(blocks)))

    # `csv` refuses fields beyond a 128 KB default. Bind it to the block ceiling instead
    # — one field may not outgrow one block — and restore it afterwards, since the
    # setting is process-global and leaking it would change every other reader's limits.
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(limits.max_block_chars)
    try:
        # newline="" keeps embedded newlines inside quoted fields intact.
        reader = csv.reader(io.StringIO(text, newline=""), dialect)
        for index, cells in enumerate(reader):
            if len(cells) > limits.csv_max_columns:
                raise DocumentTooComplexError(
                    f"CSV row has {len(cells):,} columns — the limit is {limits.csv_max_columns:,}"
                )

            if index == 0 and _looks_like_header(cells):
                header_line = render_row(cells)
                continue

            record_number += 1
            if record_number > limits.csv_max_rows:
                raise DocumentTooComplexError(
                    f"CSV has more than {limits.csv_max_rows:,} rows — split it and "
                    "upload the parts"
                )

            line = render_row(cells)
            if line.strip(" |"):
                pending.append(line)

            if record_number - block_start + 1 >= limits.csv_rows_per_block:
                flush(record_number)
                block_start = record_number + 1
    except csv.Error as exc:
        if "field larger than field limit" in str(exc):
            raise DocumentTooComplexError(f"CSV contains an oversized field: {exc}") from exc
        raise CorruptDocumentError(f"CSV could not be read: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)

    flush(record_number)

    if not blocks:
        raise NoExtractableTextError("this CSV contains no data rows")

    # `page_count` stays None — a CSV has rows, not pages (R-34).
    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks))
