"""Figure detection (T-713, R-94 §8.84 — FR-ING-09).

**The assertion that says why this module exists is the one comparing the two detectors** —
`test_a_raster_under_text_is_detected_here_and_not_by_the_ocr_detector`.
Both detectors are handed one page; `qualifying_images` returns nothing and `detect_figures`
returns the figure. That is R-94(2) as a test rather than as prose: OCR drops any region a word
overlaps so recognised text never competes with the text layer (R-88(3)/(4)), and a labelled
figure is precisely such a region.

Fixtures are generated in-process by the same library that parses them, per T-203's rule: the
suite carries no binary files and stays runnable on a clean checkout. A "figure" here is drawn as
real vector paths, because that is what the corpus that prompted the requirement contains — its
figure pages hold **zero** embedded rasters.

**The false positives are asserted, not avoided.** R-94(2) records that an uncaptioned cluster of
ruling lines is detected as a figure and that a two-panel figure may extract as two regions. Tests
below pin both as *what the detector does*, so the recorded limitation and the code cannot drift
apart — and if a later detector improves on either, the test that fails is the one naming it.

Absence assertions carry the weight here that they do in `test_tables.py`: "the page's text is
unchanged" is the only way to state that detection is additive, and a suite that checks only the
regions passes just as happily against a detector that ate the caption.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.config import ParserSettings
from app.ingestion.parsers.figures import DetectedFigure, detect_figures, render_figure
from app.ingestion.parsers.recognition import effective_dpi, qualifying_images

PROSE = "The theorem is illustrated below."
CAPTION = "FIGURE 8"

A4 = (595.0, 842.0)


def _limits(**overrides) -> ParserSettings:
    """Parser limits with recognition pinned off, never inherited from the environment.

    FR-ING-09 reads the page and never the sidecar; leaving `ocr_enabled` to `ParserSettings()`
    would let a developer's `.env` arm a container inside this module. `test_tables.py` learned
    this the same way, from T-218.
    """
    return ParserSettings(**{"ocr_enabled": False, **overrides})


def _draw_plot(page: pymupdf.Page, *, at: pymupdf.Rect) -> None:
    """A figure as a real page carries one: axes, ticks, a curve — many separate paths.

    Deliberately not one rectangle. The merge distance is the whole reason `detect_figures` sees
    a figure here rather than a dozen slivers, and a single-path fixture could not tell the two
    apart.
    """
    page.draw_line((at.x0, at.y1), (at.x1, at.y1))  # x axis
    page.draw_line((at.x0, at.y0), (at.x0, at.y1))  # y axis
    for step in range(1, 4):
        x = at.x0 + at.width * step / 4
        page.draw_line((x, at.y1 - 3), (x, at.y1 + 3))  # ticks
    page.draw_bezier(
        (at.x0 + 4, at.y1 - 6),
        (at.x0 + at.width * 0.35, at.y0 + 4),
        (at.x0 + at.width * 0.65, at.y1 - 8),
        (at.x1 - 4, at.y0 + 10),
    )


def make_page(
    *,
    figures: int = 1,
    caption: str | None = CAPTION,
    prose: bool = True,
    furniture: bool = False,
    caption_gap: float = 8.0,
    size: tuple[float, float] = A4,
) -> pymupdf.Document:
    """A born-digital page carrying `figures` vector plots, optionally captioned."""
    document = pymupdf.open()
    page = document.new_page(width=size[0], height=size[1])
    if prose:
        page.insert_text((72, 90), PROSE)
    if furniture:
        page.draw_line((0, 60), (size[0], 60))  # a header rule, full width
        page.draw_rect(pymupdf.Rect(0, 0, size[0], size[1]))  # a page border

    left = 72.0
    for _ in range(figures):
        box = pymupdf.Rect(left, 150, left + 150, 280)
        _draw_plot(page, at=box)
        if caption is not None:
            page.insert_text((box.x0, box.y1 + caption_gap), caption)
        left = box.x1 + 40
    return document


# --- what it finds ------------------------------------------------------------


def test_a_captioned_vector_figure_is_one_region_with_its_caption():
    with make_page() as document:
        found = detect_figures(document[0], limits=_limits())

    assert len(found) == 1
    assert found[0].caption == CAPTION
    # The region covers the ink and the text that belongs to it, and is nothing like the page.
    drawn = pymupdf.Rect(72, 150, 222, 280)
    assert found[0].rect.width >= drawn.width * 0.85
    assert found[0].rect.height >= drawn.height * 0.85
    assert found[0].rect.get_area() < abs(pymupdf.Rect(0, 0, *A4).get_area()) * 0.2
    # The prose at the top of the page is far away and stays out of it.
    assert found[0].rect.y0 > 100


def test_the_region_grows_to_cover_the_labels_drawn_against_the_figure():
    """Found by rendering, not by reasoning — and it is most of a figure's meaning.

    A region built from drawing operations contains the curve and the axes and **no text**,
    because a label is not a path. The first live pass over a real textbook produced exactly
    that: a plot with `y`, `x`, `f(a)`, `f(b)`, `N` and every tick label sheared off at the ink
    boundary. Nothing in a unit suite that asserts only geometry would have noticed.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(150, 300, 300, 450)
    _draw_plot(page, at=box)
    page.insert_text((box.x0 - 22, box.y0 + 20), "f(b)")  # a y-axis label, outside the ink
    page.insert_text((box.x1 + 4, box.y1 + 2), "x")  # an x-axis label, outside the ink
    with document:
        found = detect_figures(page, limits=_limits())

    assert len(found) == 1
    assert found[0].rect.x0 < box.x0 - 10, "the y-axis label is outside the region"
    assert found[0].rect.x1 > box.x1, "the x-axis label is outside the region"


def test_absorbing_labels_does_not_chain_into_the_paragraph_beside_the_figure():
    """One pass, and this is what it buys.

    Absorbing a word extends the box, so iterating would absorb the next word along and a
    figure set beside body text would walk into the paragraph and keep going. Asserted as a
    *bound on growth*, because "it did not swallow the page" is the property, and a test that
    only checked the label case would pass against a detector that swallowed everything.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(72, 300, 200, 430)
    _draw_plot(page, at=box)
    for row in range(12):
        page.insert_text((210, 310 + row * 14), "body text running down the page beside it")
    with document:
        found = detect_figures(page, limits=_limits())

    assert len(found) == 1
    # It may take the nearest words; it must not take the column.
    assert found[0].rect.x1 < 420, f"grew into the paragraph: {found[0].rect}"
    assert found[0].rect.height < box.height * 1.5


def test_a_caption_absorbed_into_the_region_is_still_reported_as_the_caption():
    """The interaction the two mechanisms have, and it silently cost 8% of captions.

    Absorption pulls a nearby caption line *inside* the region, at which point it is no longer
    "the line below this figure" and the match is lost. Measured on a real textbook the moment
    absorption landed: captioning fell from 33% to 21% of regions with nothing else changed.
    The fix is that the caption is matched against the **ink**, before growth — so this asserts
    a caption close enough to be swallowed.
    """
    with make_page(caption_gap=6.0) as document:
        found = detect_figures(document[0], limits=_limits())

    assert found[0].caption == CAPTION
    # ...and it really was swallowed, or this test would pass without the fix.
    assert found[0].rect.y1 > 280 + 6


def test_detection_leaves_the_page_text_completely_alone():
    """The largest departure from `tables.py`, and the property the feature depends on.

    A figure's caption and labels must stay in the chunk: that text is what makes the page
    retrievable, and FR-CIT-07 reaches the figure *through* a citation on that page. A detector
    that excluded its regions from extraction would make every figure it found unreachable.
    """
    with make_page() as document:
        page = document[0]
        before = page.get_text()
        detect_figures(page, limits=_limits())
        assert page.get_text() == before
        assert PROSE in before
        assert CAPTION in before


def test_a_page_of_prose_yields_nothing():
    with make_page(figures=0, prose=True) as document:
        assert detect_figures(document[0], limits=_limits()) == []


def test_an_empty_page_yields_nothing():
    with make_page(figures=0, prose=False) as document:
        assert detect_figures(document[0], limits=_limits()) == []


def test_page_furniture_is_not_a_figure():
    """A full-width hairline is a header rule or a border, never a picture."""
    with make_page(figures=0, furniture=True) as document:
        assert detect_figures(document[0], limits=_limits()) == []


def test_stacked_full_width_rules_are_page_furniture_rather_than_one_tall_figure():
    """The case the hairline filter exists for, which mutation showed nothing reached.

    A single full-width rule is already below the height floor, so dropping the filter changed
    nothing in any earlier fixture and the mutation survived. **Stacked** rules are where it
    bites: a ruled banner or a running head and foot merge into one region as wide as the page
    and tall enough to clear every floor, and without the filter every such page yields a
    "figure" that is a picture of its own furniture.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    # Thin filled bars, not `draw_line`: a stroked line has zero height, and a zero-height
    # rect is `is_empty` in PyMuPDF, so it neither merges nor grows and could never have
    # reached the filter — which is why the first version of this test still passed with the
    # filter deleted. Real PDFs draw rules both ways.
    # 10pt pitch, inside the 12pt merge reach — at 22pt they never merged and the region never
    # formed, which is the second reason the first version of this test could not fail.
    for row in range(10):
        page.draw_rect(pymupdf.Rect(0, 60 + row * 10, A4[0], 63 + row * 10))
    with document:
        assert detect_figures(page, limits=_limits()) == []


def test_an_uncaptioned_cluster_of_rules_is_detected_and_that_is_the_recorded_false_positive():
    """R-94(2), pinned rather than papered over.

    A displayed equation or a boxed sidebar drawn with ruling lines looks like a small figure,
    and the detector says so. The cost is bounded by construction — one picture nobody wanted,
    beside text that is untouched — which is *why* the floor is where it is rather than tighter.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(72, 300, 300, 400)
    page.draw_rect(box)
    page.draw_line((box.x0, box.y0 + 40), (box.x1, box.y0 + 40))
    with document:
        found = detect_figures(page, limits=_limits())

    assert len(found) == 1
    assert found[0].caption == ""


def test_two_panels_under_one_caption_extract_as_two_regions():
    """The other recorded R-94(2) limitation: `(a)` and `(b)` are separate clusters.

    Asserted as behaviour, not as an aspiration. The panels are 40pt apart and the merge
    distance is 12, so nothing here is a near-miss — closing this would mean a rule about
    captions spanning panels, which is a different detector.
    """
    with make_page(figures=2) as document:
        found = detect_figures(document[0], limits=_limits())

    assert len(found) == 2
    assert [figure.caption for figure in found] == [CAPTION, CAPTION]


# --- the reason this module is not `qualifying_images` ------------------------


def _document_with_raster(*, label: bool, twice: bool = False) -> pymupdf.Document:
    """A one-page document carrying a real embedded image, optionally with a word over it.

    A document per page, deliberately: `new_page` invalidates every `Page` object already held
    on that document, so a helper handing one back while the caller adds another is a
    use-after-invalidate — which surfaces as a bare ``page is None`` from the C layer.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 200))
    pixmap.set_rect(pixmap.irect, (200, 40, 40))
    png = pixmap.tobytes("png")
    page.insert_image(pymupdf.Rect(72, 150, 312, 350), stream=png)
    if twice:
        page.insert_image(pymupdf.Rect(72, 400, 312, 600), stream=png)
    if label:
        page.insert_text((120, 250), "y = f(x)")
    return document


def test_a_raster_under_text_is_detected_here_and_not_by_the_ocr_detector():
    """R-94(2) as an experiment: one page, two detectors, opposite answers.

    This is the whole argument for a separate module. `qualifying_images` drops the region
    *because* a word overlaps it — correctly, so recognition never competes with the text layer
    — and that is exactly the shape of every labelled figure in a textbook.
    """
    with _document_with_raster(label=True) as document:
        page = document[0]
        limits = _limits(ocr_min_image_area=0.01, ocr_min_image_pixels=10)
        assert qualifying_images(page, limits=limits) == []

        found = detect_figures(page, limits=limits)
        assert len(found) == 1
        assert found[0].rect.width == pytest.approx(240, abs=2)


def test_one_raster_placed_twice_is_two_regions_and_one_placed_once_is_one():
    """De-duplication is by `xref`, as `qualifying_images` does it — but *placements* differ.

    Two placements of one image are two pictures on the page, so the second placement is not a
    duplicate of the first; what the `xref` check prevents is the same placement being counted
    twice when PyMuPDF reports it more than once.
    """
    limits = _limits()
    with _document_with_raster(label=False) as document:
        assert len(detect_figures(document[0], limits=limits)) == 1
    with _document_with_raster(label=False, twice=True) as document:
        assert len(detect_figures(document[0], limits=limits)) == 2


# --- floors, bounds and order -------------------------------------------------


def test_a_figure_whose_parts_are_each_below_the_floor_is_one_region_after_merging():
    """Merging is what makes a figure a figure, and mutation showed nothing asserted it.

    Every earlier fixture had one path — the bezier — whose own bounding box already cleared
    the floor, so disabling the merge left the suite green while turning every real figure into
    a scatter of slivers. Here **no single part clears the floor** and only the union does.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    for index in range(4):
        top = 300 + index * 22
        page.draw_rect(pymupdf.Rect(100, top, 175, top + 14))
    with document:
        found = detect_figures(page, limits=_limits())

    assert len(found) == 1
    assert found[0].rect.height >= 60


def test_a_small_doodle_is_below_the_floor():
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    _draw_plot(page, at=pymupdf.Rect(72, 150, 102, 180))
    with document:
        assert detect_figures(page, limits=_limits()) == []


def test_the_area_fraction_floor_is_applied():
    """A region can clear both point floors and still be a trivial fraction of a large page."""
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    _draw_plot(page, at=pymupdf.Rect(72, 150, 142, 220))
    with document:
        assert len(detect_figures(page, limits=_limits(figure_min_area_fraction=0.005))) == 1
        assert detect_figures(page, limits=_limits(figure_min_area_fraction=0.5)) == []


def test_the_per_page_cap_keeps_the_largest_regions():
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    _draw_plot(page, at=pymupdf.Rect(72, 100, 172, 200))  # small
    _draw_plot(page, at=pymupdf.Rect(72, 300, 372, 600))  # large
    with document:
        found = detect_figures(page, limits=_limits(figure_max_per_page=1))

    assert len(found) == 1
    assert found[0].rect.width > 250


def test_the_cap_returns_what_it_kept_in_reading_order():
    """The final sort exists for exactly this path and no other.

    `_merge` already returns regions in reading order, so the sort at the end of
    `detect_figures` is a no-op *until* the per-page cap re-sorts by area to choose. Mutation
    found it: dropping the sort left the suite green, because no test capped more than one
    region. Here the two largest are drawn bottom-first.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    _draw_plot(page, at=pymupdf.Rect(72, 560, 372, 760))  # largest, lowest
    _draw_plot(page, at=pymupdf.Rect(72, 300, 322, 480))  # second largest, above it
    _draw_plot(page, at=pymupdf.Rect(72, 100, 172, 200))  # smallest, top — dropped by the cap
    with document:
        found = detect_figures(page, limits=_limits(figure_max_per_page=2))

    assert len(found) == 2
    assert [figure.top for figure in found] == sorted(figure.top for figure in found)
    assert found[0].top < 500 < found[1].top


def test_regions_are_returned_in_reading_order_and_two_runs_agree():
    """Ordering is geometric, never content-stream order.

    The paths are drawn bottom-up on purpose, so a detector returning them in the order PyMuPDF
    listed them would fail this.
    """
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    _draw_plot(page, at=pymupdf.Rect(72, 500, 222, 650))
    _draw_plot(page, at=pymupdf.Rect(72, 150, 222, 300))
    with document:
        first = detect_figures(page, limits=_limits())
        second = detect_figures(page, limits=_limits())

    assert [figure.top for figure in first] == sorted(figure.top for figure in first)
    assert [(f.rect, f.caption) for f in first] == [(s.rect, s.caption) for s in second]


# --- captions -----------------------------------------------------------------


def test_a_caption_beyond_the_distance_bound_is_not_read():
    with make_page(caption_gap=120.0) as document:
        found = detect_figures(document[0], limits=_limits())
    assert len(found) == 1
    assert found[0].caption == ""


def test_prose_mentioning_a_figure_is_not_a_caption():
    """The pattern is anchored and requires a number, so a sentence is not a title."""
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(72, 150, 222, 300)
    _draw_plot(page, at=box)
    # Capitalised on purpose: with a lower-case mention the pattern rejects it whether it is
    # anchored or not, so the test would pass against `search` and prove nothing (mutation).
    # 20pt below the ink, so the line is unambiguously *under* the figure and inside the
    # caption distance — otherwise the line overlaps the region vertically, is matched as
    # neither above nor below, and the test passes without the anchoring it claims to check.
    page.insert_text((box.x0, box.y1 + 20), "See Figure 8 for the curve, which is continuous")
    with document:
        found = detect_figures(page, limits=_limits())
    assert found[0].caption == ""


@pytest.mark.parametrize("caption", ["FIGURE 8", "Figure 2.3", "Fig. 4", "FIG 11"])
def test_the_declared_caption_forms_a_real_corpus_uses(caption):
    with make_page(caption=caption) as document:
        found = detect_figures(document[0], limits=_limits())
    assert found[0].caption == caption


def test_a_caption_above_loses_to_one_below():
    """Both conventions exist; a tie must not be settled by whichever line was drawn first."""
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(72, 200, 222, 350)
    _draw_plot(page, at=box)
    page.insert_text((box.x0, box.y0 - 8), "FIGURE 7")
    page.insert_text((box.x0, box.y1 + 8), "FIGURE 8")
    with document:
        found = detect_figures(page, limits=_limits())
    assert found[0].caption == "FIGURE 8"


def test_a_caption_above_is_read_when_there_is_none_below():
    document = pymupdf.open()
    page = document.new_page(width=A4[0], height=A4[1])
    box = pymupdf.Rect(72, 200, 222, 350)
    _draw_plot(page, at=box)
    page.insert_text((box.x0, box.y0 - 8), "FIGURE 7")
    with document:
        found = detect_figures(page, limits=_limits())
    assert found[0].caption == "FIGURE 7"


# --- rendering ----------------------------------------------------------------


def test_render_figure_returns_a_png_sized_by_the_figure_dpi():
    with make_page() as document:
        page = document[0]
        figure = detect_figures(page, limits=_limits())[0]
        png = render_figure(page, figure, limits=_limits())
        wider = render_figure(page, figure, limits=_limits(figure_dpi=300))

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 0
    # Measured in **pixels**: opening the PNG as a *document* reports points, because PyMuPDF
    # honours the DPI metadata written into it, so that reading is scale-blind by construction
    # and would pass against a render at any resolution.
    pixmap = pymupdf.Pixmap(png)
    assert pixmap.width == pytest.approx(figure.rect.width * 150 / 72, abs=2)
    assert len(wider) > len(png)


def test_the_render_guard_scales_an_enormous_region_down_and_leaves_a4_alone():
    """The one piece of `recognition.py` this feature reuses rather than copies.

    `get_pixmap` allocates raw samples in the worker before anything can weigh the encoded
    result, so this is the NFR-SEC-09 surface reached with an unusual mediabox and no decoder
    defect. Driven here through the *figure* numbers; `test_recognition.py` drives the same
    function through OCR's.
    """
    limits = _limits()
    a0 = pymupdf.Rect(0, 0, 2384, 3370)
    # The ceiling is stated rather than inherited: at the shipped 150 DPI even A0 is ~35M
    # pixels, under the 40M default, so a test written against the default would assert
    # nothing about scaling at all — it would pass on a guard that had been deleted.
    assert effective_dpi(a0, dpi=limits.figure_dpi, max_pixels=10_000_000) < limits.figure_dpi
    assert effective_dpi(a0, dpi=600, max_pixels=limits.figure_max_render_pixels) < 600

    a4 = pymupdf.Rect(0, 0, 595, 842)
    unscaled = effective_dpi(a4, dpi=limits.figure_dpi, max_pixels=limits.figure_max_render_pixels)
    assert unscaled == limits.figure_dpi


# --- the settings themselves --------------------------------------------------


def test_extraction_is_off_by_default_and_the_detector_does_not_read_the_flag():
    """R-94(7) is the *caller's* switch, exactly as `ocr_enabled` is.

    `detect_figures` is a pure function of the page; gating it here as well would give the
    feature two off switches that could disagree, which is the shape §8.78 warns about for the
    OCR profile and its flag.
    """
    # The *field* default, never `ParserSettings()`: constructing one reads `backend/.env`, so
    # this assertion inverted on any machine actually running the feature -- which is every
    # machine where somebody is testing it. R-94(7) is a claim about what ships, and
    # `tests/acceptance/`'s `Default` pointer reads it exactly this way, for exactly this reason.
    assert ParserSettings.model_fields["figures_enabled"].default is False
    with make_page() as document:
        assert len(detect_figures(document[0], limits=_limits(figures_enabled=False))) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"figure_dpi": 71},
        {"figure_dpi": 601},
        {"figure_max_render_pixels": 999_999},
        {"figure_min_width_points": 0},
        {"figure_min_height_points": -1},
        {"figure_min_area_fraction": 0},
        {"figure_min_area_fraction": 1.5},
        {"figure_max_per_page": 0},
        {"figure_merge_padding_points": -1},
        {"figure_caption_max_distance_points": -1},
    ],
)
def test_a_setting_that_would_silently_disable_or_unbound_the_detector_is_refused(override):
    with pytest.raises(ValueError):
        _limits(**override)


def test_detected_figure_is_frozen_so_a_caller_cannot_edit_a_region_after_the_fact():
    figure = DetectedFigure(rect=pymupdf.Rect(0, 0, 10, 10), caption="FIGURE 1")
    with pytest.raises(AttributeError):
        figure.caption = "FIGURE 2"  # type: ignore[misc]
