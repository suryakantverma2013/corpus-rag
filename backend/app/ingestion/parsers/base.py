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

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol

from app.config import ParserSettings
from app.ingestion.parsers.text import is_blank, normalize

#: Bump on **any** change to extracted text: normalisation rules, block grouping,
#: table rendering, whitespace handling. Feeds FR-ING-03's `embedding_fingerprint`,
#: so a bump forces re-embedding of affected chunks — which is the point.
#:
#: ``"2"`` (T-220, R-88(7)): Rev 0.55 changed what comes out of `pdf.py` twice over —
#: FR-ING-07 recognises pages that previously yielded nothing, and FR-ING-08 lifts a
#: table's cells out of the page's positionally-sorted text and re-renders them as
#: ``Region | Q3 | Q4`` rows. Both are new preprocessing by definition, so every stored
#: fingerprint is invalidated by construction.
#:
#: **This bump is only safe because T-608 exists.** R-76(2) recorded that no reachable
#: path re-embedded a *healthy* corpus, so before T-608 this line would have stranded
#: every existing document on a fingerprint nothing could refresh — correct, and forever.
#: The migration is `tools.reembed plan` to price it, then a bounded `run`.
#:
#: ``"3"`` (T-223, R-89): DOCX and Markdown stop folding a table into the section block
#: around it. A table-bearing document in either format changes **block grouping** — one
#: section block becomes prose / table / prose — and a Markdown table changes **table
#: rendering** too, because its cells now go through :func:`render_row` and an inner
#: whitespace run collapses the way it always has in every other format. Both are named in
#: this constant's own bump rule two paragraphs up, so the fingerprint has to move.
#:
#: **A table-free DOCX or Markdown file is byte-identical** to what shipped before, and a
#: PDF is untouched — so the real blast radius is far smaller than a version bump can
#: express. It re-embeds them anyway, because the version string is per-pipeline and not
#: per-document, and a fingerprint that were selective would have to be computed from the
#: very text it is supposed to certify.
PREPROCESSING_VERSION = "3"


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

    All three members have a producer. `pdf.py` emits ``OCR`` for a recognised page or figure
    (T-218) and is its only producer. ``TABLE`` has three: `pdf.py` for a *detected* table
    (T-219) and `docx.py` / `markdown.py` for a *declared* one (T-223) — two mechanisms, one
    marker, deliberately, because what a consumer needs to know is that the block is a grid of
    rows and not prose, which is equally true however it was found. ``TEXT`` is the default and
    `csv.py` is the one parser that only ever leaves it.

    **Not a fingerprint input** — it travels in `document_chunks.metadata`, which
    `embedding_fingerprint` does not read. That is what makes *turning recognition on* purely
    additive: an existing corpus's text blocks still hash identically and keep their vectors,
    and only the newly recognised blocks are embedded. It is deliberately not the same question
    as whether the extracted **text** changed — when it does, the fingerprint must move, which is
    why R-88(7) bumps `PREPROCESSING_VERSION` rather than leaning on this marker (T-220).
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
    #: Defaulted **and trailing** so `csv.py` — the one format that can only ever produce
    #: extracted text — needs no change. The other three set it: `pdf.py` for recognised and
    #: detected content (T-218/T-219), `docx.py` and `markdown.py` for a declared table
    #: (T-223). Set it through :func:`emit_blocks`, which takes it undefaulted on purpose.
    extraction: Extraction = Extraction.TEXT
    #: The block's own first line, when it labels every line below it — R-35's repeated CSV
    #: header generalised to FR-ING-08's tables (R-88(6)). The chunker repeats it on chunks
    #: 2..n, so a chunk of a table is never a grid of numbers with no column names.
    #:
    #: **Declared by the parser, never inferred by the chunker.** `_row_header` recovers the CSV
    #: header by counting lines against the locator's row range, which a `page` locator cannot
    #: express; and a table whose header cells are empty must not have its first *data* row
    #: repeated as one. The producer is the only thing that knows.
    header: str = ""


@dataclass(frozen=True, slots=True)
class Segment:
    """One unit of a flow-format section, waiting for the section's locator (T-223).

    `docx.py` and `markdown.py` build a section out of prose runs and declared tables in body
    order, and every one of them lands on **one** `Locator` — exactly as `compose_page` hands
    `pdf.py` an ordered `str | DetectedTable` list that lands on one page locator. This is that
    list without the geometry: `DetectedTable` carries a `Rect` because the PDF path has to keep
    a region out of the page's ordinary text, and a *declared* table has no such problem — the
    format already said where it ends.

    Three fields because that is exactly what :func:`emit_blocks` takes. Frozen, and extended
    with `dataclasses.replace`, so a half-built section cannot be mutated from two places.
    """

    text: str
    extraction: Extraction = Extraction.TEXT
    headed: bool = False


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


def render_row(cells: Iterable[str]) -> str:
    """One record on one line, cells pipe-separated, inner whitespace collapsed.

    Keeping a row on one line is what preserves the adjacency that makes it meaningful
    (`Region | Q3 | Q4`); splitting cells onto separate lines scatters a row's values across a
    chunk boundary and leaves the numbers unattributable.

    T-219 promoted this out of the parsers on the strength of a claim that was **already false**
    when it was written: it said `csv.py`, `docx.py` and `markdown.py` all rendered a row this
    way, and `markdown.py` did a bare `" | ".join` instead — so a Markdown cell kept the inner
    whitespace runs every other format collapsed, and the same table in two formats hashed
    differently for no reason a reader could see. T-223 made the claim true by repointing
    `markdown.py` here, which is one of the two text changes that bump paid for.
    """
    return " | ".join(" ".join(cell.split()) for cell in cells)


def clears_table_floor(*, rows: int, columns: int, limits: ParserSettings) -> bool:
    """Whether a table is big enough to be worth structuring (R-89).

    Counted from the source grid, never from the rendered lines: a cell containing a literal
    pipe would make a column count read back off `" | "` wrong, and re-deriving a fact from
    the serialisation of that same fact is how the two stop agreeing.

    For `pdf.py` these two floors guard a **heuristic** detector against false positives.
    `docx.py` and `markdown.py` declare their tables, so there is no false positive to guard —
    what the same numbers happen to catch there is the **layout table**, the Word idiom of
    putting paragraphs of prose in a one-row grid to position them. Promoted, that block would
    be marked `table`, and the chunker's row-atomicity rule would then forbid it the sentence
    and word separators (`chunker.py`), leaving a page of prose on one enormous line that can
    only be hard-sliced. Below the floor a table is rendered into the surrounding prose exactly
    as it was before T-223, so the cost of the floor is *structure*, never *content*.

    `table_max_per_page` is deliberately not consulted here: it bounds the per-page cost of a
    pathological layout for a detector that has to look, and these two formats have neither
    pages nor looking.
    """
    return rows >= limits.table_min_rows and columns >= limits.table_min_columns


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


def emit_blocks(
    blocks: list[ParsedBlock],
    text: str,
    *,
    locator: Locator,
    budget: CharBudget,
    limits: ParserSettings,
    extraction: Extraction,
    headed: bool = False,
) -> None:
    """Normalise, charge and append ``text`` as one or more blocks on ``locator``.

    One place for every provenance and every format, so a change to the block ceiling or to the
    budget cannot reach extracted text and miss recognised or tabular text. Promoted out of
    `pdf.py` by T-223 on :func:`render_row`'s precedent: `docx.py` and `markdown.py` would
    otherwise have been a second and third copy of an invariant that is only worth anything if
    it is identical everywhere.

    ``extraction`` is deliberately **not defaulted**, although `ParsedBlock.extraction` is. That
    default serves a caller who cannot produce anything but text; this one has three producers,
    and a default here would let a new call site label a table as prose silently — the one error
    the marker exists to make impossible.

    ``headed`` says the first line labels every line below it. The header is then re-read **from
    the normalised content** rather than carried in from the caller, which is what guarantees the
    chunker's invariant: line 0 of the block *is* `block.header`, byte for byte, so the line it
    repeats on chunks 2..n is one the user's document actually contains. :func:`split_text` may
    cut a very large table before the chunker ever sees it, so each part past the first opens
    with the header too — otherwise the split R-88(6) is written about would lose the column
    names one level above the one it names.

    **Recorded, because it is the one place this function breaks its own ceiling:** a re-prefixed
    part is `len(header) + 1` characters longer than `limits.max_block_chars` allows. Charging
    the split text instead would make the budget depend on where the splitter happened to cut,
    and shrinking the parts by the header length would make the ceiling depend on a table's
    widest row. Both are worse than a block that overshoots by one line it already contains.
    """
    content = normalize(text)
    if is_blank(content):
        return
    budget.add(content)
    header = content.split("\n", 1)[0] if headed else ""
    for index, part in enumerate(split_text(content, limits.max_block_chars)):
        blocks.append(
            ParsedBlock(
                text=f"{header}\n{part}" if header and index else part,
                locator=locator,
                order=len(blocks),
                extraction=extraction,
                header=header,
            )
        )


class Parser(Protocol):
    """What every format module exposes as ``parse``."""

    def __call__(self, payload: bytes, *, limits: ParserSettings) -> ParsedDocument: ...
