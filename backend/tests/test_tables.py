"""Tabular structure in the PDF parser (T-219, R-88(5)/(6) §8.78, FR-ING-08).

Fixtures are generated in-process by the same library that parses them, per T-203's rule: the
suite carries no binary files and stays runnable on a clean checkout. A "table" here is a real
ruled grid, because the shipped detection strategy is `lines_strict` — a whitespace-aligned
fixture would pass or fail for reasons unrelated to the code under test.

**The first assertion in this module is the one the board line asks for first**: a cell's text
must not reach the index twice. It is the failure that is invisible downstream — both chunks are
well-formed, both embed, and retrieval simply answers one passage under two citations.

Absence assertions carry real weight here. "The table's region left no text behind" is the only
way to state the exclusion, and a test that checks only the table block passes just as happily
against a parser that emits the table *and* leaves every cell in the page text.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pymupdf
import pytest

from app.config import ParserSettings, Settings
from app.ingestion.parsers import pdf as pdf_parser
from app.ingestion.parsers.base import Extraction, LocatorKind, ParsedDocument
from app.ingestion.parsers.tables import DetectedTable, compose_page, detect_tables
from app.ingestion.parsers.text import normalize
from app.services.ocr import OcrClient, OcrPageResult, OcrWord

PROSE_ABOVE = "Quarterly results follow."
PROSE_BELOW = "Notes appear after the table."
HEADER = ["Region", "Q3", "Q4"]
BODY = [["EU", "12", "15"], ["US", "20", "25"]]
GRID = [HEADER, *BODY]


# --- fixtures -----------------------------------------------------------------


def _limits(**overrides) -> ParserSettings:
    """Parser limits with recognition **pinned off**, never inherited from the environment.

    FR-ING-08 runs on the text layer and needs no sidecar (R-88(5)); leaving `ocr_enabled` to
    `ParserSettings()` would let a developer's `.env` arm it inside this module and make the
    determinism tests depend on a container. T-218 learned this the same way.
    """
    return ParserSettings(**{"ocr_enabled": False, **overrides})


def make_pdf_with_tables(
    grids: list[list[list[str]]] | None = None,
    *,
    external_header: bool = False,
    prose: bool = True,
    ruled: bool = True,
    page_height: float = 792.0,
    cell_height: float = 20.0,
    gap: float = 30.0,
) -> bytes:
    """A born-digital page carrying ruled grids, optionally with prose above and below.

    ``external_header`` draws each grid's first row as unruled text *above* the box, which is
    what makes `find_tables` report `header.external` — a real layout, and the one where the
    header sits outside `table.bbox`.
    """
    grids = [GRID] if grids is None else grids
    document = pymupdf.open()
    page = document.new_page(width=595.0, height=page_height)
    if prose:
        page.insert_text((72, 80), PROSE_ABOVE)

    top = 120.0
    width = 120.0
    for grid in grids:
        rows = grid[1:] if external_header else grid
        if external_header:
            for column, name in enumerate(grid[0]):
                page.insert_text((72 + column * width + 3, top - 6), name)
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                rect = pymupdf.Rect(
                    72 + column * width,
                    top + row_index * cell_height,
                    72 + (column + 1) * width,
                    top + (row_index + 1) * cell_height,
                )
                if ruled:
                    page.draw_rect(rect)
                page.insert_text((rect.x0 + 3, rect.y1 - 6), value)
        top += len(rows) * cell_height + gap

    if prose:
        page.insert_text((72, top + 20), PROSE_BELOW)
    data = document.tobytes()
    document.close()
    return data


def make_scanned_table_pdf(dpi: int = 150) -> bytes:
    """The same ruled table, flattened to a raster: no text layer at all."""
    rendered = pymupdf.open(stream=make_pdf_with_tables(), filetype="pdf")
    out = pymupdf.open()
    raster = rendered[0].get_pixmap(dpi=dpi).tobytes("png")
    page = out.new_page()
    page.insert_image(page.rect, stream=raster)
    rendered.close()
    data = out.tobytes()
    out.close()
    return data


class _FakeOcr(OcrClient):
    """A recogniser that replies with reading-order text. Subclass, never a Protocol (R-88(1))."""

    def __init__(self, text: str) -> None:
        super().__init__(Settings())
        self._text = text
        self.calls = 0

    def recognize(self, image: bytes) -> OcrPageResult:  # noqa: ARG002 — the raster is not read
        self.calls += 1
        return OcrPageResult(
            text=self._text,
            words=tuple(OcrWord(text=word, confidence=95.0) for word in self._text.split()),
            engine_version="5.5.0",
            languages=("eng",),
        )


def _parse(payload: bytes, *, limits: ParserSettings | None = None, **kwargs) -> ParsedDocument:
    return pdf_parser.parse(payload, limits=limits or _limits(), **kwargs)


def _blocks_of(parsed: ParsedDocument, extraction: Extraction) -> list:
    return [block for block in parsed.blocks if block.extraction is extraction]


def _text_of(parsed: ParsedDocument, extraction: Extraction) -> str:
    return "\n".join(block.text for block in _blocks_of(parsed, extraction))


def _first_page(payload: bytes):
    document = pymupdf.open(stream=payload, filetype="pdf")
    return document, document.load_page(0)


# --- the failure this feature exists to avoid ---------------------------------


def test_a_tables_cells_are_not_also_indexed_as_page_text() -> None:
    """Indexed twice, one passage answers under two citations and both chunks look healthy."""
    parsed = _parse(make_pdf_with_tables())

    page_text = _text_of(parsed, Extraction.TEXT)
    for cell in ("Region", "Q3", "EU", "12", "US", "25"):
        assert cell not in page_text, f"{cell!r} survived in the page text as well as the table"
    assert PROSE_ABOVE in page_text
    assert PROSE_BELOW in page_text


def test_an_external_header_is_not_left_behind_in_the_page_text() -> None:
    """`find_tables` places an external header *outside* `table.bbox` — hence the union.

    Excluding the bounding box alone leaves the column names in the prose while the table block
    also carries them: the same double index, one row at a time, and only on the layouts where a
    header happens to sit above the ruling.
    """
    payload = make_pdf_with_tables(external_header=True)
    document, page = _first_page(payload)
    try:
        detected = detect_tables(page, limits=_limits())
        assert len(detected) == 1
        assert page.find_tables().tables[0].header.external is True
    finally:
        document.close()

    parsed = _parse(payload)
    assert "Region" not in _text_of(parsed, Extraction.TEXT)
    assert _blocks_of(parsed, Extraction.TABLE)[0].text.startswith("Region | Q3 | Q4")


def test_an_internal_header_is_not_repeated_inside_its_own_block() -> None:
    """When the header is not external, `extract()[0]` **is** the header row."""
    parsed = _parse(make_pdf_with_tables())
    text = _blocks_of(parsed, Extraction.TABLE)[0].text
    assert text.count("Region | Q3 | Q4") == 1
    assert text.splitlines() == ["Region | Q3 | Q4", "EU | 12 | 15", "US | 20 | 25"]


# --- shape of what is emitted -------------------------------------------------


def test_a_table_is_one_block_on_its_pages_own_locator() -> None:
    """R-88(6): no fourth `LocatorKind`, and the header is declared rather than inferred."""
    parsed = _parse(make_pdf_with_tables())
    tables = _blocks_of(parsed, Extraction.TABLE)
    assert len(tables) == 1
    assert tables[0].locator.kind is LocatorKind.PAGE
    assert tables[0].locator.page == 1
    assert tables[0].header == "Region | Q3 | Q4"
    assert all(block.locator.kind is LocatorKind.PAGE for block in parsed.blocks)


def test_prose_above_and_below_a_table_keep_reading_order() -> None:
    """Interleaved, because those two paragraphs were never adjacent."""
    parsed = _parse(make_pdf_with_tables())
    assert [block.extraction for block in parsed.blocks] == [
        Extraction.TEXT,
        Extraction.TABLE,
        Extraction.TEXT,
    ]
    assert parsed.blocks[0].text == PROSE_ABOVE
    assert parsed.blocks[2].text == PROSE_BELOW
    assert [block.order for block in parsed.blocks] == [0, 1, 2]


def test_a_table_with_no_prose_still_yields_only_the_table() -> None:
    parsed = _parse(make_pdf_with_tables(prose=False))
    assert [block.extraction for block in parsed.blocks] == [Extraction.TABLE]


# --- a page without tables is untouched ---------------------------------------


def test_a_page_with_no_table_extracts_exactly_as_before() -> None:
    """Every PDF in the corpus goes through this path; it must be byte-identical to today's."""
    payload = make_pdf_with_tables(ruled=False)
    document, page = _first_page(payload)
    try:
        assert detect_tables(page, limits=_limits()) == []
        expected = normalize(page.get_text("text", sort=True))
    finally:
        document.close()

    parsed = _parse(payload)
    assert [block.extraction for block in parsed.blocks] == [Extraction.TEXT]
    assert parsed.blocks[0].text == expected


def test_an_unruled_table_is_not_detected_and_is_the_recorded_limitation() -> None:
    """The pinned `lines_strict` strategy needs ruling lines. Recorded, not discovered later."""
    parsed = _parse(make_pdf_with_tables(ruled=False))
    assert _blocks_of(parsed, Extraction.TABLE) == []
    assert "Region" in _text_of(parsed, Extraction.TEXT)


# --- thresholds degrade rather than mangle ------------------------------------


def test_a_table_below_the_row_floor_is_left_as_ordinary_text() -> None:
    parsed = _parse(make_pdf_with_tables(), limits=_limits(table_min_rows=4))
    assert _blocks_of(parsed, Extraction.TABLE) == []
    assert "Region" in _text_of(parsed, Extraction.TEXT)
    assert "12" in _text_of(parsed, Extraction.TEXT)


def test_a_table_below_the_column_floor_is_left_as_ordinary_text() -> None:
    parsed = _parse(make_pdf_with_tables(), limits=_limits(table_min_columns=4))
    assert _blocks_of(parsed, Extraction.TABLE) == []
    assert "Region" in _text_of(parsed, Extraction.TEXT)


def test_the_per_page_cap_leaves_the_surplus_tables_text_in_the_page() -> None:
    """A cap that also hid the surplus regions would delete their content instead."""
    payload = make_pdf_with_tables([GRID, GRID, GRID], cell_height=14.0, gap=20.0)
    parsed = _parse(payload, limits=_limits(table_max_per_page=1))
    assert len(_blocks_of(parsed, Extraction.TABLE)) == 1
    # The two tables that did not make the cap are still readable as text.
    assert _text_of(parsed, Extraction.TEXT).count("Region") == 2


def test_the_cap_keeps_the_tables_nearest_the_top_of_the_page() -> None:
    payload = make_pdf_with_tables([GRID, GRID, GRID], cell_height=14.0, gap=20.0)
    document, page = _first_page(payload)
    try:
        detected = detect_tables(page, limits=_limits(table_max_per_page=2))
        tops = [table.top for table in detected]
    finally:
        document.close()
    assert len(detected) == 2
    assert tops == sorted(tops)


# --- failing open (R-88(9)'s shape, one requirement over) ----------------------


def test_a_detection_fault_never_fails_a_page_that_extracted_perfectly(monkeypatch) -> None:
    """Layout analysis is an enrichment over text the page has already yielded.

    `find_tables` is a large heuristic over the most hostile input this system accepts, and it
    sits *outside* the page-level handler that makes an extraction fault a clean
    `CORRUPT_DOCUMENT`. Without its own handler a fault here escapes as an unclassified crash
    and fails a document whose characters came out perfectly — the asymmetry R-88(9) rejects
    for recognition, arriving one requirement later by a different door.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("layout analysis fell over")

    monkeypatch.setattr(pdf_parser, "detect_tables", explode)
    parsed = _parse(make_pdf_with_tables())

    assert _blocks_of(parsed, Extraction.TABLE) == []
    text = _text_of(parsed, Extraction.TEXT)
    assert PROSE_ABOVE in text
    assert "Region" in text, "the page degraded to no text at all, not to plain text"


def test_a_budget_breach_inside_a_table_page_is_still_terminal(monkeypatch) -> None:
    """Two claims at once, because one parse proves both.

    A table's characters are charged against `PARSER_MAX_EXTRACTED_CHARS` like any others —
    without that a table-dense document would bypass the ceiling entirely. And the emission sits
    **outside** the fail-open handler on purpose: `CharBudget` is R-34's caps-reject-never-truncate
    rule, and a handler written for detection faults must not swallow a terminal one.
    """
    from app.ingestion.parsers.base import DocumentTooComplexError

    with pytest.raises(DocumentTooComplexError):
        _parse(make_pdf_with_tables(), limits=_limits(max_extracted_chars=30))


# --- R-88(5): the scanned-table gap, pinned as known --------------------------


def test_a_scanned_table_yields_recognised_text_and_no_grid() -> None:
    """OCR recovers characters; it does not recover a structure the page never encoded.

    This is the case a user tests early, so R-88(5) put it in the ruling rather than a docstring
    and this pins it: a scanned table ingests as reading-order recognised text, marked `ocr`, with
    no table block anywhere.
    """
    fake = _FakeOcr("Region Q3 Q4 EU 12 15 US 20 25")
    parsed = _parse(make_scanned_table_pdf(), limits=_limits(ocr_enabled=True), recognizer=fake)
    assert fake.calls == 1
    assert _blocks_of(parsed, Extraction.TABLE) == []
    assert _blocks_of(parsed, Extraction.OCR)
    assert "Region Q3 Q4" in _text_of(parsed, Extraction.OCR)


def test_a_scanned_page_is_never_table_detected() -> None:
    """Detection reads the text layer, so a page without one must not even be asked."""
    document, page = _first_page(make_scanned_table_pdf())
    try:
        assert page.get_text("text").strip() == ""
        assert detect_tables(page, limits=_limits()) == []
    finally:
        document.close()


# --- determinism (R-88(1)) ----------------------------------------------------


def test_two_parses_of_one_file_are_byte_identical() -> None:
    """Emitted text feeds `embedding_fingerprint` through `chunk_text`; anything that varies
    between two passes over one file leaves vector reuse permanently empty (R-88(1))."""
    payload = make_pdf_with_tables([GRID, GRID], cell_height=14.0, gap=20.0)
    assert _parse(payload) == _parse(payload)


def test_composed_items_are_ordered_by_geometry() -> None:
    """`compose_page` is what decides block order, so it is what this asserts.

    `detect_tables` sorts too, but PyMuPDF already hands back geometric order — measured, drawing
    the lower grid into the content stream first — so a test aimed there would pass with the sort
    deleted and certify nothing. What the sort in `detect_tables` really pins is the cap's meaning
    ("the topmost N"), which `test_the_cap_keeps_the_tables_nearest_the_top_of_the_page` covers.
    """
    document, page = _first_page(make_pdf_with_tables([GRID, GRID], cell_height=14.0, gap=20.0))
    try:
        composed = compose_page(page, detect_tables(page, limits=_limits()))
    finally:
        document.close()

    table_tops = [item.rect.y0 for item in composed if isinstance(item, DetectedTable)]
    assert len(table_tops) == 2
    assert table_tops == sorted(table_tops)

    # **The load-bearing pair.** Asserting only "the tables are in order" passes against a
    # `compose_page` that appends every table after every prose run — the prose is already sorted
    # on the way in and `detect_tables` hands the tables over in order, so both halves hold by
    # accident. What the sort actually decides is where prose that *follows* a table lands: with
    # it, `PROSE_BELOW` is the last item; without it, it is folded into the opening run and the
    # two paragraphs either side of the tables become one.
    assert isinstance(composed[0], str) and PROSE_ABOVE in composed[0]
    assert PROSE_BELOW not in composed[0]
    assert isinstance(composed[-1], str) and PROSE_BELOW in composed[-1]


def test_the_layout_advisory_never_reaches_stdout() -> None:
    """PyMuPDF `print()`s a suggestion on first use; the worker's log stream is JSON on stdout.

    One un-parseable English line in a structured log is a defect an operator meets long after the
    run that produced it, and `set_messages()` does not suppress this one — only
    `no_recommend_layout()` does.

    **In a subprocess, and that is the whole point.** PyMuPDF fires the advisory once per
    *process*, so an in-process assertion would pass whenever anything earlier in the session had
    already spent it: vacuous, and vacuous depending on test ordering. A fresh interpreter asks the
    question exactly once, of `tables.py`'s own import-time call, which is the line that can be
    deleted. (Re-arming the private flag and reloading the module was tried first and is worse: a
    reload rebinds `DetectedTable` inside the live module dict while `pdf.py` keeps the old alias,
    so every later test in the session sees `isinstance` fail.)
    """
    script = textwrap.dedent(
        """
        import pymupdf
        from app.config import ParserSettings
        from app.ingestion.parsers.tables import detect_tables

        document = pymupdf.open()
        page = document.new_page()
        for row in range(2):
            for column in range(2):
                page.draw_rect(pymupdf.Rect(72 + column * 60, 100 + row * 20,
                                            132 + column * 60, 120 + row * 20))
                page.insert_text((75 + column * 60, 114 + row * 20), "x")
        data = document.tobytes()
        document.close()

        opened = pymupdf.open(stream=data, filetype="pdf")
        detect_tables(opened.load_page(0), limits=ParserSettings(ocr_enabled=False))
        opened.close()
        """
    )
    result = subprocess.run(  # noqa: S603 — our own interpreter, our own script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
    )
    assert result.stdout == "", f"PyMuPDF wrote to the worker's log stream: {result.stdout!r}"


# --- block-level splitting ----------------------------------------------------


def test_a_table_split_by_the_block_ceiling_repeats_its_header_on_every_part() -> None:
    """`split_text` may cut a huge table before the chunker ever sees it (R-88(6))."""
    rows = [HEADER, *[[f"R{index}", "12", "15"] for index in range(60)]]
    payload = make_pdf_with_tables([rows], cell_height=10.0, page_height=1400.0)
    parsed = _parse(payload, limits=_limits(max_block_chars=200))

    parts = _blocks_of(parsed, Extraction.TABLE)
    assert len(parts) > 1
    for part in parts:
        assert part.text.startswith("Region | Q3 | Q4")
        assert part.header == "Region | Q3 | Q4"


# --- settings -----------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"table_min_rows": 0},
        {"table_min_columns": 0},
        {"table_max_per_page": 0},
    ],
)
def test_a_table_threshold_below_one_is_refused_at_boot(override) -> None:
    """Zero is not "unbounded" for any of these — it is a feature silently switched off."""
    with pytest.raises(ValueError):
        ParserSettings(**override)
