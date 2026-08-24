"""Recognition mechanism for the PDF parser — FR-ING-07 (T-218, R-88 §8.78).

**Mechanism only.** Which page gets recognised, in what order blocks land, and what fails
open versus closed is the *requirement*, and it stays in `pdf.py` beside the docstring
R-88(10) obliges someone to keep honest. What lives here is the arithmetic: how a page
becomes a raster, which embedded images are worth the trip, what the R-88(8) floor accepts,
and how the R-88(11) bound is counted. All of it is pure enough to test without a socket.

**This module belongs to the parsers, not to `app/services/`.** `app.services.ocr` explains
the split from the other side: the API process probes the sidecar's readiness and must be
able to import that client without dragging in PyMuPDF. Everything here imports PyMuPDF, so
it is worker-side by construction (R-31).

**There is no recogniser Protocol here, deliberately.** R-88(1) refuses a pluggable seam —
the only alternative vendor class is the hosted-vision one it excludes on determinism
grounds — so `pdf.py` takes a concrete :class:`~app.services.ocr.OcrClient` and a test double
is a *subclass* of it. That is honest about what is being substituted.

**Determinism is a correctness property here, not a nicety** (R-88(1)): recognised text
becomes `chunk_text`, which feeds `embedding_fingerprint`, so anything that varies between
two passes over one file leaves `plan_chunk_set`'s vector reuse permanently empty and turns
every re-ingestion into a silent full re-embed. Hence a fixed DPI, one colorspace, no alpha,
a scale-down rule that is a function of the page's own mediabox, and an image ordering
derived from geometry rather than from content-stream order.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import pymupdf
import structlog

from app.config import ParserSettings
from app.services.ocr import OcrPageResult

log = structlog.get_logger(__name__)

#: Rendering is RGB with no alpha, pinned here rather than exposed as a setting. A colorspace
#: knob would be a fingerprint input wearing plain clothes — flipping it silently rewrites
#: every OCR'd chunk's text. Measured on this hardware: greyscale at 300 DPI is ~20% faster
#: and produced identical text, which is an argument for changing this constant deliberately
#: one day, not for letting a deployment change it.
_COLORSPACE = pymupdf.csRGB

#: PDF user space is 72 units to the inch; the ratio to `PARSER_OCR_DPI` is the scale factor.
_POINTS_PER_INCH = 72.0


@dataclass(slots=True)
class RecognitionBudget:
    """The R-88(11) bound: a page ceiling and a whole-document wall clock.

    A *limit object* rather than a timeout imposed from outside, and that is not a style
    choice — `app.ingestion.parsers` records that `asyncio.to_thread` cannot be cancelled, so
    a clock wrapped around the parse returns control to the caller while the thread runs on.
    The only bound that actually bounds anything is one the loop consults itself.

    The ceiling is sized (see `ParserSettings.ocr_max_pages`) to bind **before** the clock on
    a nominal document, because a ceiling is a deterministic function of the document and a
    clock is not. When the clock does fire it is logged, never silent.
    """

    max_passes: int
    budget_seconds: float
    spent: int = 0
    started: float = field(default_factory=time.monotonic)
    exhausted_by: str | None = None

    @classmethod
    def for_(cls, limits: ParserSettings) -> RecognitionBudget:
        return cls(max_passes=limits.ocr_max_pages, budget_seconds=limits.ocr_budget_seconds)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def allows(self) -> bool:
        """True while another recognition pass is permitted.

        Checked **before** the pass, so overshoot is bounded by one in-flight call rather
        than by an unbounded tail — which is what makes `budget + OCR_TIMEOUT_SECONDS` the
        true worst case that `Settings._coherent` refuses to let exceed the job timeout.
        """
        if self.spent >= self.max_passes:
            self.exhausted_by = self.exhausted_by or "pages"
            return False
        if self.elapsed >= self.budget_seconds:
            self.exhausted_by = self.exhausted_by or "seconds"
            return False
        return True

    def spend(self) -> None:
        """Charge one pass — including a failed one, which cost time all the same."""
        self.spent += 1


def has_renderable_content(page: pymupdf.Page) -> bool:
    """True when the page holds anything a recogniser could possibly read.

    A genuinely blank separator page — and a long scan has many — must cost no raster, no
    socket round trip and, above all, no ceiling spend. Without this a 700-page document with
    690 blank pages would exhaust `PARSER_OCR_MAX_PAGES` on nothing and never reach the ten
    pages that carry content.

    Vector drawings count as well as images. Outlined text is real (logos, some CJK exports,
    engineering drawings) and R-88 states no carve-out for it; the cost of being wrong is one
    wasted pass on a page whose result is legitimately empty. `get_cdrawings` rather than
    `get_drawings` because the question is `any()`, and the latter builds a Python object per
    path on what may be a chart-heavy page.
    """
    if page.get_image_info():
        return True
    return bool(page.get_cdrawings())


def qualifying_images(page: pymupdf.Page, *, limits: ParserSettings) -> list[pymupdf.Rect]:
    """R-88(4): the embedded rasters on a *textual* page that are worth recognising.

    Most images in a document corpus are logos, rules and decorative marks, so recognising
    every one is unbounded work for no gain. An image qualifies only above a minimum fraction
    of the page area **and** a minimum native pixel dimension — and the two are read off
    different properties on purpose: the fraction from the placement box, the dimension from
    the stored image. Measuring both from one of them is wrong in opposite directions (a
    full-page scan dropped into a stamp-sized box; a 40x40 icon stretched page-wide).

    **Regions overlapped by the text layer are excluded, and that is the subtle one.** Text
    drawn over an image would otherwise be indexed twice — once as extracted characters, once
    as recognised ones — handing retrieval two citations for one passage and violating
    R-88(3) at region granularity even though the page-level rule was honoured. The exclusion
    is deliberately conservative: *any* intersecting word drops the region, because R-88(3)
    says the text layer wins, and a figure lost to a stray overlapping label costs less than a
    duplicated passage.

    The result is sorted by geometry, never by content-stream order, so two runs over one file
    cannot produce different block orders (R-88(1)).
    """
    page_area = abs(page.rect.get_area())
    if page_area <= 0:
        return []

    words = [pymupdf.Rect(word[:4]) for word in page.get_text("words")]

    seen: set[int] = set()
    candidates: list[tuple[float, float, int, pymupdf.Rect]] = []
    for info in page.get_image_info(xrefs=True):
        # The same xref placed twice on one page is one image: recognise it once, or both the
        # cost and the emitted text double for no new content.
        xref = int(info.get("xref", 0))
        if xref and xref in seen:
            continue

        native = min(int(info.get("width", 0)), int(info.get("height", 0)))
        if native < limits.ocr_min_image_pixels:
            continue

        rect = pymupdf.Rect(info["bbox"]) & page.rect
        if rect.is_empty or abs(rect.get_area()) / page_area < limits.ocr_min_image_area:
            continue
        if any(not (rect & word).is_empty for word in words):
            continue

        if xref:
            seen.add(xref)
        candidates.append((round(rect.y0, 2), round(rect.x0, 2), xref, rect))

    candidates.sort(key=lambda item: item[:3])
    return [rect for *_, rect in candidates]


def effective_dpi(rect: pymupdf.Rect, *, dpi: int, max_pixels: int) -> int:
    """The DPI to render ``rect`` at, scaled down when it would allocate too much.

    `get_pixmap` allocates raw samples **in the worker process**, before anything can weigh
    the encoded result — so `OCR_MAX_IMAGE_BYTES`, which inspects the PNG, is far too late to
    help. A4 at 300 DPI is a harmless ~26 MB; an A0 page at the same DPI is over a gigabyte,
    and the worker parses several documents concurrently. This is the NFR-SEC-09 surface
    reached with no decoder defect at all, just an unusual mediabox.

    Scaling down rather than skipping keeps an oversized page searchable, and the rule is a
    pure function of the rect and the settings, so it cannot make one run disagree with
    another (R-88(1)).

    **Parameterised rather than reading `ParserSettings` (T-713).** FR-ING-09 renders figure
    regions at its own, lower DPI under its own ceiling, and the allocation hazard is
    identical — so the guard takes the two numbers instead of being copied beside a second
    set of field names. The OCR call site passes exactly what it passed before.
    """
    width_in = abs(rect.width) / _POINTS_PER_INCH
    height_in = abs(rect.height) / _POINTS_PER_INCH
    projected = width_in * height_in * dpi * dpi
    if projected <= 0 or projected <= max_pixels:
        return dpi
    scaled = int(dpi * math.sqrt(max_pixels / projected))
    return max(1, scaled)


def render_png(page: pymupdf.Page, *, clip: pymupdf.Rect | None, limits: ParserSettings) -> bytes:
    """Rasterise ``page`` (or ``clip`` within it) to PNG bytes for the sidecar.

    Returns **bytes and nothing else** on purpose: a `Pixmap` is a handle into the document's
    MuPDF context, and `pdf.py` closes that document in a `finally`. Anything derived from it
    that outlived the block would be a use-after-free in C rather than a Python error.
    """
    target = clip if clip is not None else page.rect
    pixmap = page.get_pixmap(
        dpi=effective_dpi(target, dpi=limits.ocr_dpi, max_pixels=limits.ocr_max_render_pixels),
        clip=clip,
        colorspace=_COLORSPACE,
        alpha=False,
    )
    try:
        return pixmap.tobytes("png")
    finally:
        del pixmap


def accepted_text(result: OcrPageResult, *, floor: float) -> str | None:
    """The recognised text, or ``None`` when R-88(8) says it must not be stored.

    Low-confidence output is not merely useless: FR-CIT-06 shows the user a **quote**, so a
    garbled passage damages trust in the citation mechanism itself, and it fills the index
    with tokens nobody would ever type. A page below the floor contributes no block, exactly
    as an empty page does.

    The ``None`` case is handled before the comparison and is not the same case. `OcrClient`
    answers ``mean_confidence is None`` for "nothing was recognised" and ``0.0`` for
    "recognised, and garbage" — a blank page is not a garbled one, and letting the first fall
    into a ``< floor`` comparison would be a ``TypeError`` rather than a decision.
    """
    confidence = result.mean_confidence
    if confidence is None:
        return None
    if confidence < floor:
        return None
    text = result.text.strip()
    return text or None
