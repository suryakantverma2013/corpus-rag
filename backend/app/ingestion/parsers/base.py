"""Parser output contract and failure taxonomy (T-203, ruling R-34).

Every parser returns the same shape — a sequence of :class:`ParsedBlock`, each carrying a
:class:`Locator` — so the chunker (T-204), the retriever (T-206) and the FR-CIT-03 hover
card can stay format-agnostic. The locator is the part that actually matters downstream:
it is what turns "this text came from somewhere" into "p. 14 of handbook.pdf", and it is
persisted verbatim into `document_chunks.metadata` by T-204.

**Locator kinds are per-format facts, not a style choice** (R-34):

* ``PAGE`` — PDF only. It is the sole accepted format with true pagination; the spec's
  own example ("p. 14") is a page.
* ``SECTION`` — DOCX and MD. Both are flow formats whose page breaks are decided by a
  renderer, not stored in the file, so a page number would be fabricated. The stable
  addressable unit is the heading path (plus MD source lines, which are stable).
* ``ROWS`` — CSV. A row range, 1-based, header excluded.

The ``label`` strings are display copy and remain §8.4 TBDs; the *fields* are normative.

:data:`PREPROCESSING_VERSION` is an input to FR-ING-03's
``embedding_fingerprint = SHA-256(chunk_text | embedding_model | chunking_version |
preprocessing_version)``. Parsing *is* the preprocessing stage, so the constant lives
here: change how text comes out of these modules and every affected chunk must re-embed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol

from app.config import ParserSettings

#: Bump on **any** change to extracted text: normalisation rules, block grouping,
#: table rendering, whitespace handling. Feeds FR-ING-03's `embedding_fingerprint`,
#: so a bump forces re-embedding of affected chunks — which is the point.
PREPROCESSING_VERSION = "1"


# --- failures -----------------------------------------------------------------


class ParseError(Exception):
    """A document could not be parsed.

    ``code`` is the machine-readable reason T-207 writes to `knowledge_jobs.error_code`
    when it moves the document to FR-ING-01 `FAILED`; ``str(exc)`` is the human message
    for `documents.error_message`. Every code is terminal — none of these become
    parseable on a retry, so T-207 must not schedule a backoff for them (unlike an
    unreachable ClamAV, which is retryable by R-32).
    """

    code: ClassVar[str] = "PARSE_FAILED"


class UnsupportedDocumentError(ParseError):
    """No parser is registered for this extension (should be unreachable post-T-202)."""

    code: ClassVar[str] = "UNSUPPORTED_FORMAT"


class CorruptDocumentError(ParseError):
    """The container is malformed — truncated, damaged, or not what it claims."""

    code: ClassVar[str] = "CORRUPT_DOCUMENT"


class EncryptedDocumentError(ParseError):
    """The document needs a password. Corpus never prompts for one."""

    code: ClassVar[str] = "ENCRYPTED_DOCUMENT"


class NoExtractableTextError(ParseError):
    """The document parsed cleanly but yields no text.

    Overwhelmingly a scanned/image-only PDF. This fails the document rather than
    producing a zero-chunk `ACTIVE` one (R-34): a document the KB list shows as ready
    but that can never be retrieved or cited is a worse lie than an explicit failure.
    OCR is deferred (T-211).
    """

    code: ClassVar[str] = "NO_EXTRACTABLE_TEXT"


class DocumentTooComplexError(ParseError):
    """A parser limit was exceeded — page/row/character count or DOCX expansion.

    The R-31(3) compensating control. Rejecting rather than truncating is deliberate:
    a partially ingested document makes retrieval confidently answer "that is not in
    your documents" about text the user did upload.
    """

    code: ClassVar[str] = "CONTENT_LIMIT_EXCEEDED"


# --- locators -----------------------------------------------------------------


class LocatorKind(StrEnum):
    """What kind of address the locator holds (R-34)."""

    PAGE = "page"
    SECTION = "section"
    ROWS = "rows"


@dataclass(frozen=True, slots=True)
class Locator:
    """Where a block came from inside its document.

    Only the fields relevant to ``kind`` are populated; :meth:`as_metadata` drops the
    rest so the JSONB stays readable.
    """

    kind: LocatorKind
    label: str  # TBD(§8.4) — display copy for the FR-CIT-03 hover card
    page: int | None = None  # 1-based
    section_path: tuple[str, ...] = ()
    section_index: int | None = None  # 1-based ordinal among sections
    row_start: int | None = None  # 1-based, header row excluded
    row_end: int | None = None
    line_start: int | None = None  # 1-based source lines (MD)
    line_end: int | None = None

    def as_metadata(self) -> dict:
        """Locator fields as they are stored in `document_chunks.metadata` (T-204)."""
        data: dict = {"kind": self.kind.value, "label": self.label}
        if self.page is not None:
            data["page"] = self.page
        if self.section_path:
            data["section_path"] = list(self.section_path)
        if self.section_index is not None:
            data["section_index"] = self.section_index
        if self.row_start is not None:
            data["row_start"] = self.row_start
            data["row_end"] = self.row_end
        if self.line_start is not None:
            data["line_start"] = self.line_start
            data["line_end"] = self.line_end
        return data


def page_locator(page: int) -> Locator:
    """PDF page locator. The label is the spec's own FR-CIT-03 example."""
    return Locator(kind=LocatorKind.PAGE, label=f"p. {page}", page=page)  # TBD(§8.4)


def rows_locator(row_start: int, row_end: int) -> Locator:
    """CSV data-row range, 1-based and header-exclusive."""
    label = f"row {row_start}" if row_start == row_end else f"rows {row_start}–{row_end}"
    return Locator(
        kind=LocatorKind.ROWS,
        label=label,  # TBD(§8.4)
        row_start=row_start,
        row_end=row_end,
    )


def section_locator(
    section_path: tuple[str, ...],
    section_index: int,
    *,
    fallback_label: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> Locator:
    """DOCX/MD section locator.

    ``fallback_label`` covers content that precedes the first heading, where the two
    formats differ in what reads naturally: DOCX has paragraph ordinals, MD has source
    lines.

    The *label* shows only the deepest two levels — a real specification nests five deep
    and the FR-CIT-03 hover card is 330px wide, so the full chain would wrap the card to
    a paragraph. The complete ``section_path`` is kept in the metadata for anything that
    wants it.
    """
    label = "§ " + " › ".join(section_path[-2:]) if section_path else fallback_label  # TBD(§8.4)
    return Locator(
        kind=LocatorKind.SECTION,
        label=label,
        section_path=section_path,
        section_index=section_index,
        line_start=line_start,
        line_end=line_end,
    )


# --- parsed document ----------------------------------------------------------


class Extraction(StrEnum):
    """How a block's characters came to exist (R-88(7), §8.78).

    Provenance rather than address, which is why it sits on the block and not on
    :class:`Locator`: an OCR'd figure and the surrounding text share one page, so they share
    one locator, and they must still be tellable apart. It is what makes an OCR-quality
    complaint diagnosable at all — without it, "the citation is garbled" cannot be
    distinguished from "the document is garbled".

    ``TABLE`` is declared here and emitted by nothing yet: FR-ING-08 is T-219's, and a
    consumer that has to grow a third case later is worse than one that already has it.

    Not a fingerprint input — it travels in `document_chunks.metadata`, which
    `embedding_fingerprint` does not read. Enabling recognition on an existing corpus is
    therefore purely additive: every text block still hashes identically and keeps its vector,
    and only the newly recognised blocks are embedded. The fleet-wide re-embed belongs to
    T-220's `PREPROCESSING_VERSION` bump, not to this marker.
    """

    TEXT = "text"
    OCR = "ocr"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """One addressable unit of text. T-204 chunks *within* blocks, never across them."""

    text: str
    locator: Locator
    order: int  # 0-based position in the document
    #: Defaulted **and trailing** so the three formats that can only ever produce extracted
    #: text — DOCX, MD, CSV — need no change. `pdf.py` is the one parser that sets it.
    extraction: Extraction = Extraction.TEXT


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """The complete parse result."""

    suffix: str
    blocks: tuple[ParsedBlock, ...] = ()
    #: → `documents.page_count`. PDF only; `None` for the flow and tabular formats,
    #: where any page number would be invented (R-34).
    page_count: int | None = None
    preprocessing_version: str = PREPROCESSING_VERSION

    @property
    def char_count(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    @property
    def text(self) -> str:
        """Whole-document text. Diagnostics and tests only — chunking works per block."""
        return "\n\n".join(block.text for block in self.blocks)


# --- shared helpers -----------------------------------------------------------


@dataclass(slots=True)
class CharBudget:
    """Running total of extracted characters against `PARSER_MAX_EXTRACTED_CHARS`.

    Checked as blocks are produced rather than at the end, so a 50 MB CSV that expands
    past the ceiling stops early instead of after building the whole list.
    """

    limit: int
    used: int = field(default=0)

    def add(self, text: str) -> None:
        self.used += len(text)
        if self.used > self.limit:
            raise DocumentTooComplexError(
                f"document expands to more than {self.limit:,} characters of text"
            )


def split_text(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into parts of at most ``max_chars``, preferring line boundaries.

    Parts keep their parent's locator — both halves of a long page really are on p. 14 —
    so this bounds block size without inventing addresses. The chunker (T-204) subdivides
    further; this only stops one pathological block from dominating memory.
    """
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > max_chars:  # a single line longer than the cap: hard slice
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(line[:max_chars])
            line = line[max_chars:]
        if size + len(line) + 1 > max_chars and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return [part for part in parts if part.strip()]


class Parser(Protocol):
    """What every format module exposes as ``parse``."""

    def __call__(self, payload: bytes, *, limits: ParserSettings) -> ParsedDocument: ...
