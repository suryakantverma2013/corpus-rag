"""The FR-ING-09 extraction pass — detector output to storable figures (T-714, R-94(4)(5)).

T-713 tested the *geometry*; this tests the pass that runs it over a whole document and turns
each region into bytes with an identity. Three properties carry the weight:

* **off is off** — with `PARSER_FIGURES_ENABLED=false` nothing opens, nothing renders and
  nothing is returned, which is R-94(7) as a test rather than as a default;
* **the id is the content** (R-94(5)) — two runs over the same bytes produce the same ids, which
  is the whole basis for T-715 setting a long immutable cache lifetime;
* **every failure yields fewer figures and never an exception**, because FR-ING-09 fails open and
  the caller is a document that is already `ACTIVE`.

Fixtures are the T-713 module's, imported rather than copied: a second generator would drift from
the detector's own tests and the divergence would look like a detector change.
"""

from __future__ import annotations

import pytest

from app.config import ParserSettings
from app.ingestion.figures import (
    ExtractedFigure,
    FigureBudget,
    extract_figures,
    extract_figures_sync,
    png_dimensions,
)
from tests.test_figures import CAPTION, make_page


def _limits(**overrides) -> ParserSettings:
    """Extraction on, recognition off — never inherited from a developer's `.env`."""
    return ParserSettings(**{"figures_enabled": True, "ocr_enabled": False, **overrides})


def _pdf(**page_kwargs) -> bytes:
    with make_page(**page_kwargs) as document:
        return document.tobytes()


# --- the off switch -----------------------------------------------------------


def test_extraction_disabled_yields_nothing_and_never_opens_the_document(monkeypatch):
    """R-94(7). Asserted through PyMuPDF, because "returns ()" is also what a broken pass does.

    The switch has to be checked *before* the open, or a 300 MB upload pays for a parse of a
    feature nobody enabled. Patching `pymupdf.open` to explode is the only way to tell the two
    apart from the outside.
    """
    import app.ingestion.figures as module

    # Built *before* the patch: the fixture generator opens PyMuPDF too, and patching first
    # fails this test against a perfectly correct off switch.
    payload = _pdf()

    # **Recorded, not raised — and mutation testing is what said so.** The first version of
    # this test raised `AssertionError` from the patched `open`, which the pass's own
    # fail-open handler catches (`AssertionError` is an `Exception`) and converts into the
    # empty tuple the test then asserted. It passed against a switch moved *after* the open,
    # which is the one defect it exists to catch. A counter cannot be swallowed.
    opened: list[str] = []

    def _record(*args, **kwargs):
        opened.append("open")
        raise RuntimeError("unreachable with extraction disabled")

    monkeypatch.setattr(module.pymupdf, "open", _record)

    assert (
        extract_figures_sync(payload, filename="a.pdf", limits=_limits(figures_enabled=False)) == ()
    )
    assert opened == [], "the document was opened although extraction is disabled"


@pytest.mark.parametrize("filename", ["notes.docx", "table.csv", "readme.md", "no-suffix"])
def test_a_format_without_pages_yields_nothing(filename: str):
    """A figure is a region of a page before it is anything else (R-34's rule, one step on)."""
    assert extract_figures_sync(_pdf(), filename=filename, limits=_limits()) == ()


# --- what it produces ---------------------------------------------------------


def test_a_captioned_figure_becomes_one_extracted_figure_with_readable_png():
    figures = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits())

    assert len(figures) == 1
    figure = figures[0]
    assert figure.page_number == 1  # 1-based, matching `Locator.page` — FR-CIT-07's join key
    assert figure.index == 0
    assert figure.caption == CAPTION
    assert figure.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert figure.width_px > 0 and figure.height_px > 0
    assert figure.byte_size == len(figure.png)
    assert figure.x1 > figure.x0 and figure.y1 > figure.y0


def test_the_id_is_the_content_so_two_runs_agree():
    """R-94(5): an unchanged crop keeps the URL a browser cached.

    The claim is about *re-ingestion*, so the test re-runs the whole pass over the same bytes
    rather than hashing one object twice — which would assert only that `hashlib` is a function.
    """
    first = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits())
    second = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits())

    assert [f.content_sha256 for f in first] == [f.content_sha256 for f in second]
    assert len(first[0].content_sha256) == 64


def test_two_figures_on_a_page_are_ordered_and_separately_identified():
    figures = extract_figures_sync(_pdf(figures=2), filename="book.pdf", limits=_limits())

    assert [f.index for f in figures] == [0, 1]
    assert figures[0].content_sha256 != figures[1].content_sha256


# --- the bounds ---------------------------------------------------------------


def test_the_per_document_ceiling_stops_the_pass():
    figures = extract_figures_sync(
        _pdf(figures=3), filename="book.pdf", limits=_limits(figure_max_per_document=2)
    )

    assert len(figures) == 2


def test_an_exhausted_clock_stops_the_pass_without_failing_it():
    """Zero is not configurable (`_coherent` refuses it), so the budget is driven directly.

    A wall clock that has already run out must yield *no* figures and *no* exception: over
    either bound the pass stops and keeps what it has, because FR-ING-09 fails open.
    """
    budget = FigureBudget(max_figures=10, budget_seconds=0.0)

    assert budget.allows() is False
    assert budget.exhausted_by == "seconds"


def test_the_ceiling_is_reported_as_the_reason_before_the_clock_is_consulted():
    budget = FigureBudget(max_figures=0, budget_seconds=1_000.0)

    assert budget.allows() is False
    assert budget.exhausted_by == "figures"


def test_a_figure_over_the_byte_cap_is_dropped_not_truncated():
    """R-34(5)'s caps-reject-never-truncate rule, applied to a picture.

    Driven through the real render at a cap of one byte: half a PNG is not a smaller picture,
    so the only correct output is one figure fewer.
    """
    figures = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits(figure_max_bytes=1))

    assert figures == ()


# --- failing open -------------------------------------------------------------


def test_bytes_that_are_not_a_pdf_yield_nothing_rather_than_raising():
    """The parse opened these bytes already, so reaching here is a defect in this process.

    It still may not raise: the document is `ACTIVE` and answering by the time this runs.
    """
    assert extract_figures_sync(b"not a pdf at all", filename="book.pdf", limits=_limits()) == ()


def test_a_page_whose_detection_raises_costs_that_page_and_no_more(monkeypatch):
    import app.ingestion.figures as module

    calls: list[int] = []

    def _explode(page, **kwargs):
        calls.append(1)
        raise RuntimeError("geometry went wrong")

    monkeypatch.setattr(module, "detect_figures", _explode)
    figures = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits())

    assert figures == ()
    assert calls, "the detector was never reached, so nothing was proven about failing open"


def test_a_render_that_raises_costs_one_figure_and_keeps_the_others(monkeypatch):
    import app.ingestion.figures as module

    real = module.render_figure
    seen: list[int] = []

    def _flaky(page, figure, *, limits):
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("pixmap allocation failed")
        return real(page, figure, limits=limits)

    monkeypatch.setattr(module, "render_figure", _flaky)
    figures = extract_figures_sync(_pdf(figures=2), filename="book.pdf", limits=_limits())

    assert len(figures) == 1


def test_unreadable_render_output_drops_the_figure(monkeypatch):
    """A row carrying dimensions nobody read would be worse than no row at all."""
    import app.ingestion.figures as module

    monkeypatch.setattr(module, "render_figure", lambda *a, **k: b"GIF89a not a png")

    assert extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits()) == ()


# --- the PNG header reader ----------------------------------------------------


def test_png_dimensions_reads_the_ihdr():
    figures = extract_figures_sync(_pdf(), filename="book.pdf", limits=_limits())
    size = png_dimensions(figures[0].png)

    assert size == (figures[0].width_px, figures[0].height_px)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x89PNG\r\n\x1a\n",  # signature only — nothing to unpack
        b"GIF89a" + b"\x00" * 32,  # right length, wrong format
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x10",  # width 0
    ],
)
def test_png_dimensions_refuses_what_it_cannot_read(payload: bytes):
    assert png_dimensions(payload) is None


# --- the async facade ---------------------------------------------------------


@pytest.mark.anyio
async def test_the_async_facade_returns_what_the_blocking_one_does():
    payload = _pdf()
    limits = _limits()

    assert [
        f.content_sha256 for f in await extract_figures(payload, filename="b.pdf", limits=limits)
    ] == [f.content_sha256 for f in extract_figures_sync(payload, filename="b.pdf", limits=limits)]


# --- the R-94(4) boundary -----------------------------------------------------


def test_an_extracted_figure_carries_no_text_of_the_document():
    """R-94(4): a figure is presentation data, so nothing on it may reach the index.

    The fields are a page, an ordinal, four floats, a caption the *document* declared, and a
    raster. There is deliberately no extracted-text field for a chunker to find.
    """
    fields = set(ExtractedFigure.__slots__)

    assert fields == {
        "page_number",
        "index",
        "x0",
        "y0",
        "x1",
        "y1",
        "caption",
        "png",
        "width_px",
        "height_px",
    }
