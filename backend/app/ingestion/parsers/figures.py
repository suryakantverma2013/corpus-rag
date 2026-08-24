"""Figure detection for the PDF parser — FR-ING-09 (T-713, R-94 §8.84).

**A third detector on the same page, and the reason it is not one of the other two.** OCR
(`recognition.py`) recovers characters that are absent; table extraction (`tables.py`) recovers
structure that was never encoded; this recovers **the picture itself** — the thing that was never
characters at all and never will be. What it produces is not a chunk: a figure takes no embedding,
carries no text into retrieval and is no part of `embedding_fingerprint` (R-94(4)), so nothing here
can change a single vector, and R-88(1)'s byte-reproducibility constraint does not reach it.

**Why `qualifying_images` could not simply be reused, which is the finding that shaped this
module (R-94(2)).** That function drops **any region a word overlaps**, deliberately, so recognised
text never competes with the text layer (R-88(3)/(4)). A labelled figure — axes, curve, `y = f(x)`,
a caption — is *precisely* such a region. The two features want opposite things from one page. And
measured on the corpus that prompted the requirement, a 1,421-page calculus textbook, the pages
carrying figures hold **zero embedded rasters**: 31, 8 and 76 *vector drawing* operations on three
consecutive pages, the figures drawn as paths and their labels drawn as text. Reusing the OCR
detector would have returned nothing at all, on every page that matters.

**Nothing here excludes anything from the page's text, and that is the largest departure from
`tables.py`.** `DetectedTable.rect` exists to keep a table's cells *out* of the ordinary prose, or
the same characters reach the index twice. A figure has the opposite requirement: its caption and
its axis labels must stay in the chunk, because that text is what makes the page **retrievable** —
and a figure nobody can retrieve is a figure nobody is ever shown. So detection here is additive
and side-effect free; a region rejected below costs a picture, never a word.

**Mechanism only**, on `recognition.py`'s and `tables.py`'s precedent: which regions become
figures, what is stored and what a citation renders is the requirement, and it lives in `pdf.py`,
the persistence layer and the GUI. What lives here is the geometry.

**Detection is a heuristic over hostile input and its failures are named rather than discovered.**
An uncaptioned cluster of ruling lines — a displayed equation, a boxed sidebar — is a false
positive, and a two-panel figure may extract as two regions under one caption. Both are recorded in
R-94(2), asserted in `tests/test_figures.py` as what the detector *does*, and cheap by construction:
the cost of a wrong region is one picture nobody wanted, next to text that is unaffected.

**Determinism.** Regions are ordered by geometry, never by content-stream order, and the caption
rule is a pure function of the page. The fingerprint argument does not apply (R-94(4)), but a
detector whose output order moved between runs would churn every stored figure's identity on
re-ingestion for no reason at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from app.config import ParserSettings
from app.ingestion.parsers.recognition import effective_dpi

#: Figures are rendered for a person to look at, so RGB — the same colorspace OCR pins, for an
#: entirely different reason (there it must not move, because it feeds recognised text into a
#: fingerprint; here it is simply what a picture is).
_COLORSPACE = pymupdf.csRGB

#: A caption the document *declares*: a line that **begins** with FIGURE/Fig and a number, so
#: the phrase "see Figure 3" inside a paragraph is not one. **The pattern is code
#: rather than a setting**: a regex knob is configuration nobody can falsify, and this decides
#: what a user is shown as the document's own words.
#: Anchoring is `match`'s job and only `match`'s: the pattern carried a leading `^` as well,
#: which made the two redundant — neither could be changed alone, so neither could be
#: *tested* alone, and a mutation removing either left the suite green (R-49's rule: delete
#: the duplicate rather than write a test that cannot fail).
_CAPTION = re.compile(r"(?:FIGURE|Figure|FIG|Fig)\.?\s*\d+", re.ASCII)

#: A drawing whose longest side is under this contributes nothing on its own — a dot, a tick, a
#: speck of a dashed rule. It is not a *figure* floor (that is `figure_min_*_points`, applied
#: after merging): axes and tick marks are legitimately thin, and dropping them before the merge
#: would shrink the very region they bound.
_MIN_SEGMENT_POINTS = 4.0

#: Page furniture: a rule as wide as the page but only points high is a header line, a footer
#: line or a border, never a figure. Applied to *drawings* only — a full-page raster is exactly
#: what a scanned page looks like.
_FURNITURE_WIDTH_FRACTION = 0.9
_FURNITURE_MAX_HEIGHT_POINTS = 6.0

#: A drawn rectangle covering essentially the whole page is a **page border**, and it is the one
#: that matters: unfiltered it merges with everything inside it and turns every page into a
#: single figure the size of the page. Drawings only, for the reason above. The cost is named:
#: a genuinely full-page vector chart is refused, which is rarer than a border by a wide margin
#: and is the trade R-94(2)'s false-positive discussion accepts in the other direction.
_FURNITURE_MIN_PAGE_COVERAGE = 0.95


@dataclass(frozen=True, slots=True)
class DetectedFigure:
    """One region of a page worth showing as a picture, and what the page calls it."""

    #: The region to render, clipped to the page.
    rect: pymupdf.Rect
    #: The document's own caption line — ``"FIGURE 8"``, ``"Figure 2.3 A damped spring"`` — or
    #: ``""`` when the page declares none. Never synthesised: R-34 refused to invent a page
    #: number for a format that has none, and the same rule governs a name.
    caption: str

    @property
    def top(self) -> float:
        return self.rect.y0


def detect_figures(page: pymupdf.Page, *, limits: ParserSettings) -> list[DetectedFigure]:
    """The figures on ``page``, in reading order.

    Two sources, one output: clusters of vector drawing operations, and embedded rasters
    **without** the text-overlap exclusion that makes `qualifying_images` the wrong tool here.
    A page with neither yields ``[]`` and costs a fraction of a millisecond.
    """
    page_area = abs(page.rect.get_area())
    if page_area <= 0:
        return []

    ink = _merge(_drawing_rects(page) + _raster_rects(page), limits=limits)
    # Each region is carried as (what to render, the ink it grew from). The caption is matched
    # against the **ink**, not the grown box: absorption pulls a nearby caption line *inside*
    # the region, at which point it is no longer "the line below this figure" and the match is
    # silently lost. Measured — captioning fell from 33% to 21% of regions on a real textbook
    # the moment absorption landed, with nothing else changed.
    accepted = [
        (grown, source)
        for grown, source in zip(_absorb_labels(ink, page, limits=limits), ink, strict=True)
        if grown.width >= limits.figure_min_width_points
        and grown.height >= limits.figure_min_height_points
        and abs(grown.get_area()) / page_area >= limits.figure_min_area_fraction
    ]

    # Bounding a pathological page keeps the *largest* regions rather than the first ones: on a
    # page of many small clusters the big ones are the figures, and "first" would be an accident
    # of geometry the cap was never meant to express. Ties break on position, so the choice is
    # still a pure function of the page.
    if len(accepted) > limits.figure_max_per_page:
        accepted.sort(key=lambda pair: (-abs(pair[0].get_area()), _order(pair[0])))
        accepted = accepted[: limits.figure_max_per_page]

    lines = _caption_lines(page)
    accepted.sort(key=lambda pair: _order(pair[0]))
    return [
        DetectedFigure(rect=grown, caption=_caption_for(source, lines, limits=limits))
        for grown, source in accepted
    ]


def render_figure(page: pymupdf.Page, figure: DetectedFigure, *, limits: ParserSettings) -> bytes:
    """Rasterise ``figure``'s region to PNG bytes.

    Returns **bytes and nothing else**, for `render_png`'s reason: a `Pixmap` is a handle into
    the document's MuPDF context, which `pdf.py` closes in a `finally`, so anything derived from
    it that outlived the block would be a use-after-free in C rather than a Python error.

    The resolution guard is `recognition.effective_dpi` — the same function OCR uses, given this
    feature's numbers. `get_pixmap` allocates raw samples in the worker process before anything
    can weigh the encoded result, so a hand-crafted mediabox is a memory-exhaustion vector with
    no decoder defect involved; over the ceiling the render scales down deterministically rather
    than being skipped.
    """
    pixmap = page.get_pixmap(
        dpi=effective_dpi(
            figure.rect,
            dpi=limits.figure_dpi,
            max_pixels=limits.figure_max_render_pixels,
        ),
        clip=figure.rect,
        colorspace=_COLORSPACE,
        alpha=False,
    )
    try:
        return pixmap.tobytes("png")
    finally:
        del pixmap


def _order(rect: pymupdf.Rect) -> tuple[float, float]:
    """Reading order, rounded so a sub-point jitter cannot reorder two runs."""
    return (round(rect.y0, 2), round(rect.x0, 2))


def _drawing_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Vector paths worth clustering, page furniture removed.

    A zero-width vertical axis or zero-height horizontal one is `is_empty` by PyMuPDF's
    definition and is kept anyway: it is part of the figure it bounds, and discarding it would
    shrink the region by exactly the axis the reader needs to see.
    """
    width_limit = page.rect.width * _FURNITURE_WIDTH_FRACTION
    page_area = abs(page.rect.get_area())
    out: list[pymupdf.Rect] = []
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"]) & page.rect
        if max(rect.width, rect.height) < _MIN_SEGMENT_POINTS:
            continue
        if rect.width >= width_limit and rect.height <= _FURNITURE_MAX_HEIGHT_POINTS:
            continue
        if page_area > 0 and abs(rect.get_area()) / page_area >= _FURNITURE_MIN_PAGE_COVERAGE:
            continue
        out.append(rect)
    return out


def _raster_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Embedded images, **without** `qualifying_images`' text-overlap exclusion (R-94(2)).

    **De-duplicated by placement, not by `xref`, and that is a real difference from the OCR
    detector.** There, one image placed twice must be recognised once, because recognising it
    twice puts the same characters in the index twice. Here two placements are two pictures on
    the page and both are worth showing; what has to be collapsed is the *same* placement
    reported more than once, which is a question about geometry.
    """
    seen: set[tuple[float, float, float, float]] = set()
    out: list[pymupdf.Rect] = []
    for info in page.get_image_info(xrefs=True):
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        if rect.is_empty:
            continue
        key = (round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(rect)
    return out


def _merge(rects: list[pymupdf.Rect], *, limits: ParserSettings) -> list[pymupdf.Rect]:
    """Union rectangles that lie within the merge distance of one another.

    A figure arrives as many independent paths — a curve, two axes, tick marks, shading, an
    arrowhead — so without this every figure is a scatter of slivers and every floor in
    :func:`detect_figures` rejects all of them.

    Iterated to a fixed point rather than in one pass: merging A with B can bring the union
    within reach of C, and a single pass would leave a figure split in two on nothing more
    principled than the order PyMuPDF listed its paths.
    """
    padding = limits.figure_merge_padding_points
    merged = sorted(rects, key=_order)
    for _ in range(len(merged)):
        out: list[pymupdf.Rect] = []
        for rect in merged:
            grown = pymupdf.Rect(
                rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y1 + padding
            )
            union = rect
            keep: list[pymupdf.Rect] = []
            for other in out:
                if grown.intersects(other):
                    union |= other
                else:
                    keep.append(other)
            keep.append(union)
            out = keep
        if len(out) == len(merged):
            return sorted(out, key=_order)
        merged = sorted(out, key=_order)
    return merged


def _absorb_labels(
    rects: list[pymupdf.Rect], page: pymupdf.Page, *, limits: ParserSettings
) -> list[pymupdf.Rect]:
    """Grow each region to cover the words drawn on and against it.

    **Found by rendering, and it is the difference between a figure and a fragment.** A region
    built from drawing operations alone contains the curve and the axes and *none of the text*,
    because a label is not a path: the first live pass over a real textbook produced a plot with
    `y`, `x`, `f(a)`, `f(b)`, `N` and every tick label sheared off at the ink boundary. For a
    mathematical figure that is most of the meaning.

    **One pass, deliberately.** Absorbing a word extends the box, so iterating would absorb the
    next word along, and a figure set beside body text would walk into the paragraph and keep
    going. A single pass against the *original* region grows it by at most the reach of one
    word, which is the label case exactly.

    The reach is `figure_merge_padding_points` rather than a knob of its own: "these marks
    belong to one figure" and "this label belongs to that figure" are the same judgement about
    the same page, and a second distance would be two numbers nobody could tune apart.
    """
    words = [pymupdf.Rect(word[:4]) for word in page.get_text("words")]
    pad = limits.figure_merge_padding_points
    out: list[pymupdf.Rect] = []
    for rect in rects:
        reach = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        union = pymupdf.Rect(rect)
        for word in words:
            if word.intersects(reach):
                union |= word
        out.append(union & page.rect)
    return out


def _caption_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str]]:
    """Every text line on the page that *reads* as a caption, with its box.

    Read from `get_text("dict")` rather than `"blocks"`: a block can hold a caption and the
    paragraph beneath it, and a caption is a line.
    """
    out: list[tuple[pymupdf.Rect, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text and _CAPTION.match(text):
                out.append((pymupdf.Rect(line["bbox"]), text))
    return out


def _caption_for(
    rect: pymupdf.Rect,
    lines: list[tuple[pymupdf.Rect, str]],
    *,
    limits: ParserSettings,
) -> str:
    """The nearest declared caption to ``rect``, or ``""``.

    Below is preferred over above because that is the convention in every corpus this was
    measured against, and a tie between the two would otherwise be settled by nothing. Having no
    caption is a legitimate outcome and never a rejection: a figure is worth showing whether or
    not the document names it, and R-34's refusal to synthesise a locator is the same rule.
    """
    limit = limits.figure_caption_max_distance_points
    best: tuple[float, str] | None = None
    for box, text in lines:
        if box.x1 <= rect.x0 or box.x0 >= rect.x1:
            continue
        below = box.y0 - rect.y1
        above = rect.y0 - box.y1
        if 0 <= below <= limit:
            distance = below
        elif 0 <= above <= limit:
            # Ranked behind an equally distant caption underneath, never ahead of it.
            distance = above + limit + 1
        else:
            continue
        if best is None or distance < best[0]:
            best = (distance, text)
    return "" if best is None else best[1]
