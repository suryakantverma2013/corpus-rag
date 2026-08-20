"""Tabular structure for the PDF parser — FR-ING-08 (T-219, R-88(5)/(6) §8.78).

**A separate mechanism from FR-ING-07, with a separate trigger.** OCR recovers characters that
are absent; this recovers *structure* that was never encoded, from characters that are already
present. A born-digital table therefore needs no recognition at all — running OCR over one would
discard perfect text for a re-derivation, which R-88(3) forbids — so everything here reads the
**text layer** and none of it waits on the sidecar.

**Mechanism only**, on `recognition.py`'s precedent: which regions become table blocks, in what
order they land and what degrades is the requirement, and it stays in `pdf.py`. What lives here is
the arithmetic — detection, serialisation, and the geometry that keeps a table's cells out of the
page's ordinary text.

**The serialised form is the one three parsers already share:** one row per line, cells joined by
``" | "``. `csv.py`, `docx.py` and `markdown.py` each render a row that way, and R-88(6) presumes
it when it inherits R-35's rule that a split table repeats its header — a form with no header line
would have nothing to repeat. The header is *declared* on the block rather than inferred from it
(:attr:`~app.ingestion.parsers.base.ParsedBlock.header`), because a table whose header cells are
empty must not have its first data row repeated as one.

**Detection is deliberately conservative and its strategy is pinned.** PyMuPDF's default
(`lines_strict`) requires ruling lines, so a page of prose yields nothing and the common case costs
a fraction of a millisecond. **Recorded limitation, beside R-88(5)'s scanned-table gap and for the
same reason — it is the case a user tests early:** a *whitespace-aligned* table with no ruling
lines is not detected, and ingests as today's positionally-sorted text. The `text` strategy would
find those, and would also find "tables" in any column-ish layout; the cost of a false positive is
a page of prose rewritten as a grid, so it is not offered as a setting. **Revisit trigger:** a
corpus where unruled tables are measurably the loss — the change is then a strategy *and* a
false-positive corpus to govern it, not a flag.

**There is no `PARSER_TABLE_ENABLED`.** R-88(12) gives recognition an off switch because it adds a
container, a language pack and a large latency term; none of that applies here — this is PyMuPDF,
already a dependency — so R-79(5)'s test governs instead: FR-ING-08 says *shall*, and a switch
that stopped it would turn a requirement off. The thresholds below are the tuning levers.

**Determinism is a correctness property** (R-88(1)): the emitted text feeds `embedding_fingerprint`
through `chunk_text`, so tables are ordered by **geometry, never by content-stream order**, and
every rule here is a pure function of the page.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from app.config import ParserSettings
from app.ingestion.parsers.base import render_row

#: PyMuPDF prints ``Consider using the pymupdf_layout package…`` to **stdout** on the first
#: `find_tables()` call of a process — a bare `print()`, so it survives `set_messages()`. The
#: worker's structlog stream is JSON on stdout, so that one English line lands mid-stream and is
#: un-parseable to anything reading the logs. This is the documented switch for it.
pymupdf.no_recommend_layout()

#: Index of the block type in a ``get_text("blocks")`` tuple; ``0`` is text, ``1`` an image.
_BLOCK_TYPE = 6


@dataclass(frozen=True, slots=True)
class DetectedTable:
    """One accepted table: what to emit, and what to keep out of the page's ordinary text."""

    #: The region excluded from text extraction — the table's bounding box **united with an
    #: external header's**, since `find_tables` places a header above the box when it detects
    #: one there. Excluding only the box would leave that row in the prose *and* in this block.
    rect: pymupdf.Rect
    #: The rendered header line, or ``""`` when the table has no usable one. Line 0 of
    #: :attr:`text` when non-empty.
    header: str
    #: The serialised table: header (if any) then one line per non-empty row.
    text: str

    @property
    def top(self) -> float:
        return self.rect.y0


def _rendered_rows(table: pymupdf.table.Table) -> tuple[str, list[str]]:
    """``(header_line, body_lines)`` for ``table``, blank rows dropped.

    Two facts about `header.external` are load-bearing and neither is obvious. It reports whether
    the header sits *outside* the table's bounding box — which decides the exclusion region — and
    it equally decides whether ``extract()[0]`` **is** that header: when it is not external, row 0
    is the header row, and emitting it as a body line prints the column names twice.
    """
    header = getattr(table, "header", None)
    names = [name or "" for name in (header.names if header is not None else [])]
    header_line = render_row(names) if any(name.strip() for name in names) else ""

    rows = table.extract()
    if header_line and header is not None and not header.external and rows:
        rows = rows[1:]

    body = [line for line in (render_row(row) for row in rows) if line.strip(" |")]
    return header_line, body


def _exclusion_rect(table: pymupdf.table.Table, page: pymupdf.Page) -> pymupdf.Rect:
    rect = pymupdf.Rect(table.bbox)
    header = getattr(table, "header", None)
    if header is not None and header.external and header.bbox is not None:
        rect |= pymupdf.Rect(header.bbox)
    return rect & page.rect


def detect_tables(page: pymupdf.Page, *, limits: ParserSettings) -> list[DetectedTable]:
    """The tables on ``page`` worth extracting as structure, in reading order.

    Detection is heuristic, so every rejection here leaves the page exactly as it was: a table
    below the row or column floor, or past the per-page cap, is simply not detected, and its
    characters stay in the ordinary text. **That is what makes a false positive degrade rather
    than mangle a page** — and it is why only *accepted* tables contribute an exclusion region.
    A cap that hid the surplus tables' regions would delete their content instead of leaving it.
    """
    detected: list[DetectedTable] = []
    for table in page.find_tables().tables:
        header_line, body = _rendered_rows(table)
        if not body:
            continue
        # Counted from the lines actually emitted rather than from `row_count`, which does not
        # include an external header — the two disagree on exactly the tables the floor decides.
        if len(body) + (1 if header_line else 0) < limits.table_min_rows:
            continue
        if table.col_count < limits.table_min_columns:
            continue
        rect = _exclusion_rect(table, page)
        if rect.is_empty:
            continue
        lines = [header_line, *body] if header_line else body
        detected.append(DetectedTable(rect=rect, header=header_line, text="\n".join(lines)))

    # Defensive, and measured rather than assumed: PyMuPDF returns tables in geometric order
    # today whatever order the content stream drew them in, so this changes nothing for the
    # engine we ship — it is here to pin what the cap below *means*. `detected[:N]` has to be
    # "the topmost N", and reading that guarantee off an undocumented library behaviour is how
    # a version bump silently starts keeping different tables. `compose_page` owns the ordering
    # that reaches the output, and that one is load-bearing.
    detected.sort(key=lambda item: (round(item.rect.y0, 2), round(item.rect.x0, 2)))
    return detected[: limits.table_max_per_page]


def compose_page(page: pymupdf.Page, tables: Sequence[DetectedTable]) -> list[str | DetectedTable]:
    """The page's prose runs and its tables, interleaved in reading order.

    Prose is re-derived from ``get_text("blocks")`` because the table's cells must come out of it
    — indexed twice, they hand retrieval two citations for one passage — and blocks are the
    coarsest unit carrying a bounding box to test. Any intersection drops a block, deliberately
    conservative on `qualifying_images`' precedent: a caption lost to a table that touches it
    costs less than a duplicated passage.

    Interleaving rather than "all prose, then all tables" matters for one concrete reason: the
    paragraphs above and below a mid-page table were never adjacent, and concatenating them
    invents a sentence boundary that the chunker would then embed.
    """
    # `Sequence`, not `Iterable`: this walks ``tables`` twice — once for the regions, once to
    # place them — and a generator would quietly yield an empty region list on the second
    # pass, which is the double-index defect with no symptom at the call site.
    items: list[tuple[float, float, int, str | DetectedTable]] = []
    regions = [table.rect for table in tables]

    for block in page.get_text("blocks", sort=True):
        if block[_BLOCK_TYPE] != 0:  # an image block carries no characters
            continue
        rect = pymupdf.Rect(block[:4])
        if any(not (rect & region).is_empty for region in regions):
            continue
        # 0 sorts a prose run before a table sharing its top edge, which only a hand-built
        # PDF achieves — but a tie broken by object identity is not deterministic.
        items.append((round(rect.y0, 2), round(rect.x0, 2), 0, block[4]))

    for table in tables:
        items.append((round(table.rect.y0, 2), round(table.rect.x0, 2), 1, table))

    items.sort(key=lambda item: item[:3])

    composed: list[str | DetectedTable] = []
    run: list[str] = []
    for *_, item in items:
        if isinstance(item, DetectedTable):
            if run:
                composed.append("\n".join(run))
                run = []
            composed.append(item)
        else:
            run.append(item)
    if run:
        composed.append("\n".join(run))
    return composed
