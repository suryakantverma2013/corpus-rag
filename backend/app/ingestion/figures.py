"""Figure extraction — the pass that turns T-713's detector into stored pictures (T-714).

**A second open of the same bytes, deliberately.** `pdf.py` could have carried figures out on
`ParsedDocument` — it already has the pages loaded — and that is exactly why it must not. That
dataclass is the fingerprint-bearing contract: everything on it describes the *text* a chunk is
built from, and `embedding_fingerprint` is computed downstream from what it carries. R-94(4)
says a figure is presentation data that feeds no embedding and forces no re-embed, and the
cheapest way to hold a rule like that is to make breaking it unrepresentable rather than to
remember it — so a figure never travels through the parse result at all. The cost is one extra
`pymupdf.open`; the per-page detection dominates either way.

**It runs after the swap, and nothing here may fail a document.** FR-ING-09 fails open: a page
whose figures cannot be detected or rendered contributes none, and the document ingests exactly
as it does today. That is the inverse of the parse above it, which fails closed — R-88(9)'s
split again, and for the same reason: extraction produces the text a document *is*, while this
produces a picture beside it.

**The bounds are limit objects, not a clock around the call.** `app.ingestion.parsers` records
why: `asyncio.to_thread` cannot be cancelled, so a timeout wrapped around this returns control
to the caller while the thread runs to completion. The only bound that bounds anything is one
this loop consults itself.

**Determinism matters here, for a reason that is not the fingerprint's.** A figure's id is the
SHA-256 of its own PNG (R-94(5)), so a re-ingestion producing the same crop produces the same id
and a URL a browser cached stays valid. Nothing in this module may depend on iteration order,
wall-clock time or the machine it ran on.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
from dataclasses import dataclass, field

import pymupdf
import structlog

from app.config import ParserSettings, get_settings
from app.ingestion.parsers import pdf as pdf_parser
from app.ingestion.parsers.figures import DetectedFigure, detect_figures, render_figure
from app.security.content_validation import normalized_suffix

log = structlog.get_logger(__name__)

#: PNG's 8-byte signature, then the IHDR length and type; width and height are the two
#: big-endian uint32s at offset 16. Read here rather than by widening `render_figure` to return
#: a triple, which would cost T-713 its contract — **bytes and nothing else**, because a
#: `Pixmap` is a handle into a MuPDF context the caller closes in a `finally`.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_OFFSET = 16


@dataclass(frozen=True, slots=True)
class ExtractedFigure:
    """One rendered figure, ready to be stored (T-714).

    Carries its own bytes: the pass renders inside the MuPDF context and the caller stores
    outside it, so there is no handle in between that could outlive the document.
    """

    #: 1-based, matching `Locator.page` — FR-CIT-07's join key and the reason this is the one
    #: field whose numbering may never quietly change.
    page_number: int
    #: 0-based ordinal within the page, in the detector's reading order.
    index: int
    #: PDF points, as the detector found them. Provenance, and the answer to a future
    #: crop-accuracy question; nothing at serve time reads them.
    x0: float
    y0: float
    x1: float
    y1: float
    #: The document's own caption line, or ``""`` when the page declares none. Never
    #: synthesised — R-34 refused to invent a page number for a format that has none, and the
    #: same rule governs a name.
    caption: str
    png: bytes
    width_px: int
    height_px: int

    @property
    def content_sha256(self) -> str:
        """The figure's public id (R-94(5)).

        Derived from content, so an unchanged crop keeps its URL across a re-ingestion — which
        is the whole reason T-715's route may set a long immutable cache lifetime. It is
        deliberately **not** the row's primary key: the same crop can legitimately appear on
        two pages of one version, and two rows would then collide on an id that is doing its
        job correctly.
        """
        return hashlib.sha256(self.png).hexdigest()

    @property
    def byte_size(self) -> int:
        return len(self.png)


@dataclass(slots=True)
class FigureBudget:
    """The whole-document bound: a figure ceiling and a wall clock.

    `RecognitionBudget`'s shape, and the ceiling is likewise the bound *meant* to bind on a
    nominal document — a count is a deterministic function of the input, a clock is a function
    of the box. The clock exists for the case the count cannot see: a 1,400-page document with
    no figures in it spends nothing and would otherwise page through to the end whatever that
    costs.
    """

    max_figures: int
    budget_seconds: float
    spent: int = 0
    started: float = field(default_factory=time.monotonic)
    exhausted_by: str | None = None

    @classmethod
    def for_(cls, limits: ParserSettings) -> FigureBudget:
        return cls(
            max_figures=limits.figure_max_per_document,
            budget_seconds=limits.figure_budget_seconds,
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def allows(self) -> bool:
        """True while more work is permitted.

        Checked **before** the work rather than after it, so overshoot is bounded by one page
        rather than by an unbounded tail — which is what makes the budget the true worst case
        `Settings._coherent` weighs against the job timeout.
        """
        if self.spent >= self.max_figures:
            self.exhausted_by = self.exhausted_by or "figures"
            return False
        if self.elapsed >= self.budget_seconds:
            self.exhausted_by = self.exhausted_by or "seconds"
            return False
        return True

    def spend(self) -> None:
        self.spent += 1


def png_dimensions(png: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG's IHDR, or ``None`` if these are not readable PNG bytes.

    ``None`` rather than an exception because the caller's answer to both is the same — drop
    the figure — and because a renderer returning something unreadable is a defect in this
    process, never a reason to fail a user's document.
    """
    if not png.startswith(_PNG_SIGNATURE) or len(png) < _IHDR_OFFSET + 8:
        return None
    width, height = struct.unpack(">II", png[_IHDR_OFFSET : _IHDR_OFFSET + 8])
    if width <= 0 or height <= 0:
        return None
    return width, height


def extract_figures_sync(
    payload: bytes, *, filename: str, limits: ParserSettings | None = None
) -> tuple[ExtractedFigure, ...]:
    """Every figure in ``payload``, in document order. Blocking, pure, no I/O.

    Returns ``()`` — costing one comparison and nothing else — when the feature is off or the
    format has no figures to find. PDF is the only format with a page to render a region of:
    R-34 refused to invent a page number for the flow formats, and a figure is a region of a
    page before it is anything else.
    """
    limits = limits or get_settings().parser
    if not limits.figures_enabled:
        return ()
    if normalized_suffix(filename) != pdf_parser.SUFFIX:
        return ()

    try:
        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception:  # noqa: BLE001 — fails open; the parse has already opened these bytes
        log.warning("figures.open_failed", exc_info=True)
        return ()

    budget = FigureBudget.for_(limits)
    figures: list[ExtractedFigure] = []
    try:
        for index in range(document.page_count):
            if not budget.allows():
                break
            page_number = index + 1
            try:
                page = document.load_page(index)
                detected = detect_figures(page, limits=limits)
            except Exception:  # noqa: BLE001 — one page's geometry, never the document
                log.warning("figures.page_failed", page=page_number, exc_info=True)
                continue

            for ordinal, figure in enumerate(detected):
                if not budget.allows():
                    break
                # Spent whether or not the render below yields anything: the ceiling bounds
                # *work attempted*, and a page of regions that each fail to render is exactly
                # the pathological input it exists to stop.
                budget.spend()
                extracted = _render(
                    page, figure, page_number=page_number, ordinal=ordinal, limits=limits
                )
                if extracted is not None:
                    figures.append(extracted)
    finally:
        # Nothing derived from `document` may outlive this block — `render_figure` returns
        # `bytes` precisely so that cannot happen (T-713).
        document.close()

    if budget.exhausted_by:
        log.warning(
            "figures.budget_exhausted",
            exhausted_by=budget.exhausted_by,
            figures=len(figures),
            elapsed_ms=round(budget.elapsed * 1000),
        )
    return tuple(figures)


def _render(
    page: pymupdf.Page,
    figure: DetectedFigure,
    *,
    page_number: int,
    ordinal: int,
    limits: ParserSettings,
) -> ExtractedFigure | None:
    """One region to an :class:`ExtractedFigure`, or ``None`` when it does not survive.

    Three ways to yield nothing, each of them quiet by design: the render raised, the bytes are
    not a readable PNG, or the encoded picture is over `PARSER_FIGURE_MAX_BYTES`. The last is a
    **drop and never a truncation** — half a picture is not a smaller picture, and R-34(5)'s
    caps-reject-never-truncate rule reads the same way here as it does for text.
    """
    try:
        png = render_figure(page, figure, limits=limits)
    except Exception:  # noqa: BLE001 — one figure, never the page and never the document
        log.warning("figures.render_failed", page=page_number, exc_info=True)
        return None

    if len(png) > limits.figure_max_bytes:
        log.info(
            "figures.oversize_dropped",
            page=page_number,
            bytes=len(png),
            limit=limits.figure_max_bytes,
        )
        return None

    size = png_dimensions(png)
    if size is None:
        log.warning("figures.unreadable_png", page=page_number, bytes=len(png))
        return None

    return ExtractedFigure(
        page_number=page_number,
        index=ordinal,
        x0=float(figure.rect.x0),
        y0=float(figure.rect.y0),
        x1=float(figure.rect.x1),
        y1=float(figure.rect.y1),
        caption=figure.caption,
        png=png,
        width_px=size[0],
        height_px=size[1],
    )


async def extract_figures(
    payload: bytes, *, filename: str, limits: ParserSettings | None = None
) -> tuple[ExtractedFigure, ...]:
    """The async facade the T-207 worker calls; identical semantics, off the event loop.

    Rendering is CPU-bound C code over hostile input, exactly like the parse — and the worker
    runs an event loop a stalled thread would otherwise hold.
    """
    return await asyncio.to_thread(extract_figures_sync, payload, filename=filename, limits=limits)
