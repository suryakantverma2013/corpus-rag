"""Per-page recognition in the PDF parser (T-218, R-88 §8.78, FR-ING-07).

Every fixture is generated in-process by the same library that parses it, per T-203's rule:
the suite carries no binary files and stays runnable on a clean checkout. A "scan" here is a
real image-only page — text rendered to a raster and re-inserted with no text layer — which
is the distinction `make_pdf(text=False)` in `test_parsers.py` does **not** make: that
produces genuinely blank pages, and a fixture that cannot exhibit the case is the exact
defect R-88(2) filed against T-211's document-level trigger.

The double is a **subclass of `OcrClient`**, not an implementation of a Protocol. R-88(1)
refuses a pluggable recogniser seam — the only alternative vendor class is the hosted-vision
one it excludes on determinism grounds — so substituting a subclass is honest about what is
being replaced: a test fake, never a vendor.

Absence-of-call assertions carry as much weight here as presence ones. "The sidecar was never
asked" is the only way to state R-88(3) (a page with a text layer is never rasterised) and
R-88(11) (the ceiling refuses *before* the request), and a test that checks only the returned
blocks passes just as happily against a parser that recognises everything and throws the
result away.
"""

from __future__ import annotations

import math
import os
import time
import uuid

import pymupdf
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OcrSettings, ParserSettings, Settings, WorkerSettings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.repositories.documents import DocumentRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.ingestion.chunker import chunk_document_sync
from app.ingestion.incremental import persist_chunk_set, plan_chunk_set
from app.ingestion.parsers import parse_document_sync
from app.ingestion.parsers import pdf as pdf_parser
from app.ingestion.parsers.base import (
    DocumentTooComplexError,
    Extraction,
    LocatorKind,
    NoExtractableTextError,
)
from app.ingestion.parsers.recognition import (
    RecognitionBudget,
    accepted_text,
    effective_dpi,
    has_renderable_content,
    qualifying_images,
)
from app.services.embeddings import FakeEmbeddingClient
from app.services.ocr import (
    OcrClient,
    OcrImageTooLargeError,
    OcrPageResult,
    OcrProtocolError,
    OcrUnavailableError,
    OcrWord,
)

LABEL = "Quarterly revenue rose to 4,218 units."
MODEL = "text-embedding-3-large"


# --- fixtures -----------------------------------------------------------------


def _limits(**overrides) -> ParserSettings:
    """Recognition on by default here, and never inherited from the developer's `.env`."""
    return ParserSettings(**{"ocr_enabled": True, **overrides})


def _image_bytes(width: int, height: int) -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pixmap.clear_with(220)
    return pixmap.tobytes("png")


def make_scanned_pdf(pages: int = 1, *, dpi: int = 150, label: str = LABEL) -> bytes:
    """A genuine image-only PDF — rendered pages re-inserted as full-page rasters.

    Unlike `test_parsers.make_pdf(text=False)`, which yields *blank* pages, this carries
    content that only a recogniser can reach: `get_text` returns `""` and one full-page image
    covers the mediabox.
    """
    source = pymupdf.open()
    for index in range(pages):
        source.new_page().insert_text((72, 120), f"{label} Page {index + 1}.", fontsize=18)
    rendered = pymupdf.open(stream=source.tobytes(), filetype="pdf")
    source.close()

    out = pymupdf.open()
    for index in range(pages):
        raster = rendered[index].get_pixmap(dpi=dpi).tobytes("png")
        page = out.new_page()
        page.insert_image(page.rect, stream=raster)
    rendered.close()
    data = out.tobytes()
    out.close()
    return data


def make_pdf_with_figure(
    *,
    area_fraction: float = 0.30,
    native_px: int = 800,
    text: str = "Body text that the extractor can read without help.",
    over_figure: bool = False,
) -> bytes:
    """A textual page carrying one embedded raster of a chosen area and native resolution.

    ``over_figure`` draws a word inside the image's own box, which is the double-index case
    `qualifying_images` must refuse.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontsize=14)

    width = page.rect.width - 144
    height = (page.rect.get_area() * area_fraction) / width
    rect = pymupdf.Rect(72, 300, 72 + width, 300 + height)
    page.insert_image(rect, stream=_image_bytes(native_px, native_px))
    if over_figure:
        page.insert_text((rect.x0 + 20, rect.y0 + 40), "Figure 1", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def make_mixed_pdf(label: str = LABEL) -> bytes:
    """Textual, scanned, blank, textual — the shape R-88(2) exists for."""
    scan = pymupdf.open(stream=make_scanned_pdf(1, label=label), filetype="pdf")
    out = pymupdf.open()
    out.new_page().insert_text((72, 100), "First page, born digital.", fontsize=14)
    out.insert_pdf(scan, from_page=0, to_page=0)
    out.new_page()  # deliberately empty
    out.new_page().insert_text((72, 100), "Last page, born digital.", fontsize=14)
    scan.close()
    data = out.tobytes()
    out.close()
    return data


def _result(text: str, confidence: float = 95.0, *, scored: bool = True) -> OcrPageResult:
    words = (
        tuple(OcrWord(text=word, confidence=confidence) for word in text.split()) if scored else ()
    )
    return OcrPageResult(text=text, words=words, engine_version="5.5.0", languages=("eng",))


class _FakeOcr(OcrClient):
    """Records every raster it is handed and replies from a script.

    A subclass rather than a Protocol implementation — see the module docstring. The base
    ``__init__`` only reads settings; it opens no connection, so this costs nothing.
    """

    def __init__(self, *responses: OcrPageResult | Exception, delay: float = 0.0) -> None:
        super().__init__(Settings())
        self._responses = list(responses)
        self._delay = delay
        self.calls: list[bytes] = []

    def recognize(self, image: bytes) -> OcrPageResult:
        self.calls.append(image)
        if self._delay:
            time.sleep(self._delay)
        if not self._responses:
            return _result(LABEL)
        response = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _parse(payload: bytes, *, limits: ParserSettings, fake: _FakeOcr):
    return pdf_parser.parse(payload, limits=limits, recognizer=fake)


def _ocr_blocks(parsed):
    return [block for block in parsed.blocks if block.extraction is Extraction.OCR]


# --- R-88(2): the trigger is per page -----------------------------------------


def test_a_scanned_page_becomes_a_recognised_block_on_its_own_page_locator() -> None:
    fake = _FakeOcr()
    parsed = _parse(make_scanned_pdf(), limits=_limits(), fake=fake)

    assert len(fake.calls) == 1
    assert [block.extraction for block in parsed.blocks] == [Extraction.OCR]
    assert parsed.blocks[0].text == LABEL
    assert parsed.blocks[0].locator.kind is LocatorKind.PAGE
    assert parsed.blocks[0].locator.page == 1
    assert parsed.page_count == 1


def test_one_scanned_insert_inside_a_textual_pdf_is_recognised() -> None:
    """The case the document-level `if not blocks` branch could never see (R-88(2)).

    A single extractable page anywhere suppresses that branch forever, so before this task a
    scanned insert in a 700-page report was silently dropped with nothing failing.
    """
    fake = _FakeOcr()
    parsed = _parse(make_mixed_pdf(), limits=_limits(), fake=fake)

    kinds = [block.extraction for block in parsed.blocks]
    assert kinds == [Extraction.TEXT, Extraction.OCR, Extraction.TEXT]
    assert [block.locator.page for block in parsed.blocks] == [1, 2, 4]
    assert len(fake.calls) == 1, "only the scanned page should have been rasterised"


def test_block_order_is_contiguous_across_a_mixed_document() -> None:
    parsed = _parse(make_mixed_pdf(), limits=_limits(), fake=_FakeOcr())
    assert [block.order for block in parsed.blocks] == list(range(len(parsed.blocks)))


# --- R-88(3): recognition never competes with a text layer --------------------


def test_a_textual_page_is_never_rasterised() -> None:
    """R-88(3). Asserted by the *absence* of a request, which is the only honest form.

    A test that checked the produced blocks would pass just as well against a parser that
    recognises every page and discards the answer — paying the latency and the egress the
    ruling forbids, invisibly.
    """
    source = pymupdf.open()
    for index in range(3):
        source.new_page().insert_text((72, 100), f"Born-digital page {index + 1}.")
    payload = source.tobytes()
    source.close()

    fake = _FakeOcr()
    parsed = _parse(payload, limits=_limits(), fake=fake)

    assert fake.calls == []
    assert all(block.extraction is Extraction.TEXT for block in parsed.blocks)


def test_recognition_does_not_perturb_a_born_digital_document() -> None:
    """Enabling the feature must leave an existing corpus byte-identical.

    If it did not, turning the flag on would silently invalidate every stored fingerprint —
    a fleet-wide re-embed disguised as an enrichment, which is exactly what R-88(3) refuses.
    """
    source = pymupdf.open()
    source.new_page().insert_text((72, 100), "Born-digital page one.")
    payload = source.tobytes()
    source.close()

    without = pdf_parser.parse(payload, limits=ParserSettings(ocr_enabled=False))
    with_ocr = _parse(payload, limits=_limits(), fake=_FakeOcr())
    assert without == with_ocr


def test_text_drawn_over_a_figure_excludes_that_figure() -> None:
    """R-88(3) at region granularity — otherwise the caption is indexed twice.

    One passage under two citations is worse than a figure left unread, and it is invisible
    downstream: both chunks look perfectly well-formed.
    """
    fake = _FakeOcr()
    _parse(make_pdf_with_figure(over_figure=True), limits=_limits(), fake=fake)
    assert fake.calls == []


# --- R-88(4): embedded rasters on a textual page ------------------------------


def test_a_large_figure_on_a_textual_page_is_recognised() -> None:
    fake = _FakeOcr(_result("Recovered figure caption"))
    parsed = _parse(make_pdf_with_figure(), limits=_limits(), fake=fake)

    assert len(fake.calls) == 1
    ocr = _ocr_blocks(parsed)
    assert [block.text for block in ocr] == ["Recovered figure caption"]
    assert ocr[0].locator.page == 1, "a figure keeps its page's locator (R-88(6))"


def test_a_small_logo_is_ignored_without_comment() -> None:
    fake = _FakeOcr()
    _parse(make_pdf_with_figure(area_fraction=0.01), limits=_limits(), fake=fake)
    assert fake.calls == []


def test_a_large_but_low_resolution_image_is_ignored() -> None:
    """The second clause of R-88(4), and it fails differently from the first.

    Area and native resolution are read off different properties on purpose — a stretched
    40x40 icon covers half a page, and a full-page scan can sit in a stamp-sized box.
    """
    fake = _FakeOcr()
    _parse(
        make_pdf_with_figure(area_fraction=0.40, native_px=40),
        limits=_limits(),
        fake=fake,
    )
    assert fake.calls == []


def test_the_same_image_placed_twice_is_recognised_once() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Body text.", fontsize=12)
    raster = _image_bytes(800, 800)
    page.insert_image(pymupdf.Rect(72, 200, 472, 500), stream=raster)
    page.insert_image(pymupdf.Rect(72, 520, 472, 820), stream=raster)
    payload = doc.tobytes()
    doc.close()

    fake = _FakeOcr()
    _parse(payload, limits=_limits(), fake=fake)
    assert len(fake.calls) == 1


def test_qualifying_images_are_ordered_by_geometry() -> None:
    """R-88(1): block order may not depend on content-stream order."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(72, 520, 472, 800), stream=_image_bytes(700, 700))
    page.insert_image(pymupdf.Rect(72, 120, 472, 400), stream=_image_bytes(800, 800))
    payload = doc.tobytes()
    doc.close()

    with pymupdf.open(stream=payload, filetype="pdf") as reopened:
        rects = qualifying_images(reopened.load_page(0), limits=_limits())
    assert [round(rect.y0) for rect in rects] == sorted(round(rect.y0) for rect in rects)


# --- blank pages --------------------------------------------------------------


def test_a_blank_page_costs_no_round_trip() -> None:
    """A 700-page scan with 690 blank pages must not spend the ceiling on nothing."""
    doc = pymupdf.open()
    doc.new_page()
    payload = doc.tobytes()
    doc.close()

    fake = _FakeOcr()
    with pytest.raises(NoExtractableTextError):
        _parse(payload, limits=_limits(), fake=fake)
    assert fake.calls == []


def test_has_renderable_content_separates_blank_from_drawn() -> None:
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page().draw_rect(pymupdf.Rect(100, 100, 200, 200), color=(0, 0, 0))
    # Reloaded rather than reused: inserting a page invalidates `Page` handles taken before
    # it, and the symptom is an `AttributeError` deep inside PyMuPDF rather than a clear one.
    payload = doc.tobytes()
    doc.close()

    with pymupdf.open(stream=payload, filetype="pdf") as reopened:
        assert has_renderable_content(reopened.load_page(0)) is False
        assert has_renderable_content(reopened.load_page(1)) is True


# --- R-88(8): the confidence floor --------------------------------------------


def test_a_page_below_the_confidence_floor_contributes_no_block() -> None:
    fake = _FakeOcr(_result("g4rbl3d", confidence=20.0))
    with pytest.raises(NoExtractableTextError):
        _parse(make_scanned_pdf(), limits=_limits(ocr_min_confidence=60.0), fake=fake)
    assert len(fake.calls) == 1, "the page was recognised; its result was discarded"


def test_nothing_recognised_is_not_the_same_case_as_recognised_garbage() -> None:
    """`None` and `0.0` reach the same outcome down different branches (R-88(8)).

    `None` means a blank raster, `0.0` means "recognised, and garbage". Collapsing them lets
    a `None` fall into a `< floor` comparison, which is a `TypeError` rather than a decision.
    """
    assert accepted_text(_result("ghost", scored=False), floor=60.0) is None
    assert accepted_text(_result("junk", confidence=0.0), floor=60.0) is None
    assert accepted_text(_result("clean", confidence=90.0), floor=60.0) == "clean"


def test_a_floor_of_zero_still_rejects_an_unrecognised_page() -> None:
    """The `None` branch is load-bearing on its own and no floor value substitutes for it.

    At ``floor=0.0`` a collapsed branch — treating "nothing was scored" as a confidence of
    zero — would sail past the comparison and store the text. The page carries words the
    engine declined to score (Tesseract writes ``conf = -1`` on structural rows), which is
    why the text is non-empty: an empty one would be rejected by the tail of the function
    instead, and the branch could be deleted with every test still green.
    """
    assert accepted_text(_result("ghost", scored=False), floor=0.0) is None
    assert accepted_text(_result("ghost", confidence=0.0), floor=0.0) == "ghost"


# --- R-88(9): recognition fails open, the document rule fails closed ----------


@pytest.mark.parametrize(
    "failure",
    [
        OcrUnavailableError("sidecar down"),
        OcrProtocolError("garbage back"),
        OcrImageTooLargeError("too big"),
        RuntimeError("mupdf render fault"),
    ],
    ids=["unavailable", "protocol", "too-large", "render"],
)
def test_a_recognition_failure_never_fails_a_document_with_a_text_layer(failure) -> None:
    fake = _FakeOcr(failure)
    parsed = _parse(make_pdf_with_figure(), limits=_limits(), fake=fake)

    assert len(fake.calls) == 1
    assert [block.extraction for block in parsed.blocks] == [Extraction.TEXT]


def test_a_failure_on_one_page_still_ingests_the_others() -> None:
    fake = _FakeOcr(_result("page one"), OcrUnavailableError("blip"), _result("page three"))
    parsed = _parse(make_scanned_pdf(3), limits=_limits(), fake=fake)

    assert len(fake.calls) == 3
    assert [block.locator.page for block in parsed.blocks] == [1, 3]


def test_a_scan_with_a_dead_sidecar_still_fails_closed() -> None:
    """R-88(9)'s other half: R-34's no-zero-chunk-`ACTIVE` guarantee survives."""
    fake = _FakeOcr(OcrUnavailableError("sidecar down"))
    with pytest.raises(NoExtractableTextError):
        _parse(make_scanned_pdf(2), limits=_limits(), fake=fake)


def test_an_unreadable_page_still_fails_closed() -> None:
    """Extraction failure stays terminal. Only *recognition* fails open."""
    from app.ingestion.parsers.base import CorruptDocumentError

    with pytest.raises(CorruptDocumentError, match="could not be opened"):
        pdf_parser.parse(b"%PDF-1.7\n" + b"\x01\x02garbage" * 20, limits=_limits())


# --- R-88(11): the bound ------------------------------------------------------


def test_the_page_ceiling_stops_recognition_without_failing_the_document() -> None:
    fake = _FakeOcr()
    parsed = _parse(make_scanned_pdf(3), limits=_limits(ocr_max_pages=1), fake=fake)

    assert len(fake.calls) == 1, "the ceiling refuses before the request, not after it"
    assert [block.locator.page for block in parsed.blocks] == [1]


def test_the_wall_clock_budget_stops_recognition() -> None:
    fake = _FakeOcr(delay=0.05)
    parsed = _parse(make_scanned_pdf(3), limits=_limits(ocr_budget_seconds=0.01), fake=fake)
    assert len(fake.calls) == 1
    assert len(parsed.blocks) == 1


def test_a_budget_records_which_limit_stopped_it() -> None:
    pages = RecognitionBudget(max_passes=1, budget_seconds=60.0)
    pages.spend()
    assert pages.allows() is False
    assert pages.exhausted_by == "pages"

    clock = RecognitionBudget(max_passes=100, budget_seconds=0.0)
    assert clock.allows() is False
    assert clock.exhausted_by == "seconds"


# --- R-88(12): off by default -------------------------------------------------


def test_recognition_is_off_by_default() -> None:
    fake = _FakeOcr()
    with pytest.raises(NoExtractableTextError, match="needs OCR"):
        pdf_parser.parse(make_scanned_pdf(), limits=ParserSettings(), recognizer=fake)
    assert fake.calls == []


def test_a_double_cannot_smuggle_recognition_past_the_off_switch() -> None:
    """`PARSER_OCR_ENABLED` is the single gate; passing a recogniser is not consent."""
    fake = _FakeOcr()
    with pytest.raises(NoExtractableTextError):
        pdf_parser.parse(
            make_scanned_pdf(), limits=ParserSettings(ocr_enabled=False), recognizer=fake
        )
    assert fake.calls == []


# --- budgets and rendering ----------------------------------------------------


def test_recognised_text_is_charged_against_the_extraction_budget() -> None:
    """Without this a long scan bypasses `PARSER_MAX_EXTRACTED_CHARS` entirely."""
    fake = _FakeOcr(_result("x" * 500))
    with pytest.raises(DocumentTooComplexError, match="characters"):
        _parse(make_scanned_pdf(), limits=_limits(max_extracted_chars=50), fake=fake)


def test_recognised_text_obeys_the_block_ceiling() -> None:
    fake = _FakeOcr(_result(" ".join(["word"] * 200)))
    parsed = _parse(make_scanned_pdf(), limits=_limits(max_block_chars=100), fake=fake)

    blocks = _ocr_blocks(parsed)
    assert len(blocks) > 1
    assert all(len(block.text) <= 100 for block in blocks)
    assert {block.locator.page for block in blocks} == {1}


def test_the_raster_is_a_png_sized_by_the_configured_dpi() -> None:
    fake = _FakeOcr()
    _parse(make_scanned_pdf(), limits=_limits(ocr_dpi=150), fake=fake)

    raster = fake.calls[0]
    assert raster.startswith(b"\x89PNG\r\n\x1a\n")
    # A `Pixmap`, not a document: opening a PNG as a document reports its size in *points*,
    # which hands back the page geometry rather than the resolution under test.
    pixmap = pymupdf.Pixmap(raster)
    assert 1200 < pixmap.width < 1300  # A4 at 150 DPI is ~1240 px wide
    assert 1700 < pixmap.height < 1800


def test_an_oversized_page_is_scaled_down_rather_than_skipped() -> None:
    """The guard that keeps a hand-crafted mediabox from OOM-ing the worker.

    `get_pixmap` allocates before anything can weigh the encoded PNG, so the client's
    `OCR_MAX_IMAGE_BYTES` refusal is far too late to prevent it.
    """
    limits = _limits(ocr_dpi=300, ocr_max_render_pixels=4_000_000)
    a0 = pymupdf.Rect(0, 0, 3370, 2384)
    scaled = effective_dpi(a0, dpi=limits.ocr_dpi, max_pixels=limits.ocr_max_render_pixels)

    assert scaled < 300
    projected = (a0.width / 72 * scaled) * (a0.height / 72 * scaled)
    assert projected <= limits.ocr_max_render_pixels
    # An ordinary page is untouched at the shipped ceiling — the clamp is for the outlier.
    shipped = _limits()
    assert (
        effective_dpi(
            pymupdf.Rect(0, 0, 595, 842),
            dpi=shipped.ocr_dpi,
            max_pixels=shipped.ocr_max_render_pixels,
        )
        == 300
    )


def test_the_scale_down_is_a_pure_function_of_the_page() -> None:
    limits = _limits(ocr_max_render_pixels=4_000_000)
    a0 = pymupdf.Rect(0, 0, 3370, 2384)
    twice = [
        effective_dpi(a0, dpi=limits.ocr_dpi, max_pixels=limits.ocr_max_render_pixels)
        for _ in range(2)
    ]
    assert twice[0] == twice[1]


def test_recognised_text_passes_through_the_normalisation_choke_point() -> None:
    """Recognised text is normalised like every other parser's output.

    `text.py` is what makes a `.docx` and its `.pdf` export hash identically, and it is
    idempotent by construction so a re-parse of unchanged bytes cannot produce a different
    `chunk_hash`. Recognition entering downstream of it would put CRLFs and non-breaking
    spaces straight into `embedding_fingerprint` (R-88(1)).
    """
    # Code points as integers, following `app/ingestion/parsers/text.py`'s own convention:
    # a test whose behaviour depends on invisible bytes is one careless editor away from
    # passing for the wrong reason. CR LF, and a non-breaking space between the two words.
    messy = "Line one." + chr(13) + chr(10) + "Line" + chr(160) + "two.   "
    fake = _FakeOcr(_result(messy))
    parsed = _parse(make_scanned_pdf(), limits=_limits(), fake=fake)

    assert parsed.blocks[0].text == "Line one." + chr(10) + "Line two."


def test_a_degraded_run_logs_no_document_content() -> None:
    """R-43(5) and NFR-SEC-09: diagnostics carry codes and counts, never the page."""
    fake = _FakeOcr(OcrProtocolError(f"engine said: {LABEL}"))
    with structlog.testing.capture_logs() as logs:
        _parse(make_pdf_with_figure(), limits=_limits(), fake=fake)

    events = [entry for entry in logs if entry["event"] == "pdf.recognition_degraded"]
    assert len(events) == 1
    assert events[0]["reason"] == "OcrProtocolError"
    assert events[0]["recognised_passes"] == 1
    assert LABEL not in repr(events[0])


# --- the R-88(11) cross-group bound -------------------------------------------


def test_a_recognition_budget_past_the_job_timeout_is_refused_at_boot() -> None:
    """The bound spans three settings groups, so it can live on none of them.

    Recognition is bounded by the budget plus one in-flight page, and a worst case past
    `WORKER_JOB_TIMEOUT_SECONDS` does not merely run long: arq kills the job mid-pipeline and
    R-41(5)'s `stalled` flag reports a *healthy* long ingestion as stuck. Refuse to boot
    instead, on the `CLAMAV_MAX_STREAM_BYTES` precedent.
    """
    with pytest.raises(ValueError, match="PARSER_OCR_BUDGET_SECONDS"):
        Settings(
            parser=_limits(ocr_budget_seconds=900.0),
            ocr=OcrSettings(timeout_seconds=60.0),
            worker=WorkerSettings(job_timeout_seconds=900.0),
        )


def test_the_bound_is_not_enforced_while_recognition_is_off() -> None:
    """Neither knob is read with the feature off, so refusing a boot for one is noise."""
    settings = Settings(
        parser=ParserSettings(ocr_enabled=False, ocr_budget_seconds=9_000.0),
        worker=WorkerSettings(job_timeout_seconds=900.0),
    )
    assert settings.parser.ocr_budget_seconds == 9_000.0


def test_the_shipped_defaults_fit_inside_the_shipped_job_timeout() -> None:
    """Sanity on the arithmetic the defaults claim: 600 + 60 = 660 < 900."""
    # `figures_enabled` pinned: it is inherited from `backend/.env` otherwise, and the
    # coupled refusal then fires on the OCR + figures sum (960 > 900), failing this test
    # for a reason it is not about.
    settings = Settings(parser=ParserSettings(ocr_enabled=True, figures_enabled=False))
    worst_case = settings.parser.ocr_budget_seconds + settings.ocr.timeout_seconds
    assert worst_case < settings.worker.job_timeout_seconds


# --- live: the real sidecar ---------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("OCR_LIVE_TEST", "") == "",
    reason="OCR_LIVE_TEST is unset; live sidecar tests skipped",
)


@live
def test_live_a_scanned_pdf_becomes_searchable() -> None:
    parsed = parse_document_sync(make_scanned_pdf(dpi=300), filename="scan.pdf", limits=_limits())
    assert [block.extraction for block in parsed.blocks] == [Extraction.OCR]
    assert "4,218" in parsed.text


@live
def test_live_the_same_scan_parses_identically_twice() -> None:
    """R-88(1) one level above T-217's client test: it pins the *render* as well.

    Recognised text feeds `chunk_text`, which feeds `embedding_fingerprint`, so anything that
    varies between two passes leaves vector reuse permanently empty and turns every
    re-ingestion into a silent full re-embed. The positive assertion matters — two empty
    results would otherwise compare equal and pass.
    """
    payload = make_scanned_pdf(dpi=300)
    first = parse_document_sync(payload, filename="scan.pdf", limits=_limits())
    second = parse_document_sync(payload, filename="scan.pdf", limits=_limits())

    assert "4,218" in first.text
    assert first == second


@live
def test_live_a_born_digital_pdf_is_untouched_by_recognition() -> None:
    """The property that makes enabling the feature safe on an existing corpus."""
    source = pymupdf.open()
    source.new_page().insert_text((72, 100), "Born-digital page one.", fontsize=14)
    payload = source.tobytes()
    source.close()

    without = parse_document_sync(
        payload, filename="report.pdf", limits=ParserSettings(ocr_enabled=False)
    )
    with_ocr = parse_document_sync(payload, filename="report.pdf", limits=_limits())
    assert without == with_ocr


@live
def test_live_a_figure_on_a_textual_page_becomes_searchable() -> None:
    scan = pymupdf.open(stream=make_scanned_pdf(dpi=300), filename=None, filetype="pdf")
    raster = scan[0].get_pixmap(dpi=200).tobytes("png")
    scan.close()

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "See the figure below.", fontsize=14)
    side = math.sqrt(page.rect.get_area() * 0.45)
    page.insert_image(pymupdf.Rect(72, 250, 72 + side, 250 + side), stream=raster)
    payload = doc.tobytes()
    doc.close()

    parsed = parse_document_sync(payload, filename="figure.pdf", limits=_limits())
    kinds = [block.extraction for block in parsed.blocks]
    assert Extraction.TEXT in kinds and Extraction.OCR in kinds
    assert "4,218" in parsed.text


# --- R-88(1) at the level that matters: vector reuse (T-220) -------------------


async def _reuse_plan(session, chunked, *, version: int):
    """`plan_chunk_set` for ``chunked`` at ``version``, against a throwaway document."""
    user = await UserRepository(session).upsert_from_claims(
        sub=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com"
    )
    kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
    document = await DocumentRepository(session).add(
        Document(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="scan.pdf",
            storage_uri="s3://corpus/scan.pdf",
            checksum_sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.ACTIVE,
            searchable=True,
        )
    )
    client = FakeEmbeddingClient()
    plan = await plan_chunk_set(
        session=session,
        client=client,
        chunked=chunked,
        document_id=document.id,
        document_version=version,
        knowledge_base_id=kb.id,
    )
    await persist_chunk_set(session, plan=plan)
    return document, kb, client


async def test_re_ingesting_a_recognised_document_reuses_every_vector(
    session: AsyncSession,
) -> None:
    """R-88(1) stated as the property that actually costs money if it fails.

    `plan_chunk_set` reuses a stored vector by **set membership on `embedding_fingerprint`**, so
    a recogniser whose output varies between two passes over one file leaves `reused` permanently
    empty: every `/replace`, every FR-ING-04 retry and every T-608 rebuild silently re-embeds the
    whole document, and nothing fails — only the bill moves. The parse-level determinism tests
    say the text is stable; this says the pipeline *acts* on that stability.
    """
    payload = make_scanned_pdf()
    fake = _FakeOcr()
    limits = _limits()

    first = chunk_document_sync(_parse(payload, limits=limits, fake=fake), embedding_model=MODEL)
    assert [chunk.extraction for chunk in first.chunks] == [Extraction.OCR]
    document, kb, _ = await _reuse_plan(session, first, version=1)

    # A second ingestion of the *same bytes* — the shape of `/replace` and of a T-608 rebuild.
    second = chunk_document_sync(
        _parse(payload, limits=limits, fake=_FakeOcr()), embedding_model=MODEL
    )
    client = FakeEmbeddingClient()
    plan = await plan_chunk_set(
        session=session,
        client=client,
        chunked=second,
        document_id=document.id,
        document_version=2,
        knowledge_base_id=kb.id,
    )

    assert plan.added_rows == ()
    assert len(plan.reused_rows) == plan.total == len(second.chunks) > 0
    assert plan.embedded_inputs == 0
    assert client.embedded_inputs == 0, "a recognised document was re-embedded for no reason"


@live
async def test_live_re_ingesting_a_real_scan_reuses_every_vector(session: AsyncSession) -> None:
    """The same property against the real engine, which is the only place it can actually break."""
    payload = make_scanned_pdf(dpi=300)
    limits = _limits()

    first = chunk_document_sync(
        parse_document_sync(payload, filename="scan.pdf", limits=limits), embedding_model=MODEL
    )
    document, kb, _ = await _reuse_plan(session, first, version=1)

    second = chunk_document_sync(
        parse_document_sync(payload, filename="scan.pdf", limits=limits), embedding_model=MODEL
    )
    client = FakeEmbeddingClient()
    plan = await plan_chunk_set(
        session=session,
        client=client,
        chunked=second,
        document_id=document.id,
        document_version=2,
        knowledge_base_id=kb.id,
    )

    assert plan.added_rows == ()
    assert len(plan.reused_rows) == plan.total > 0
    assert client.embedded_inputs == 0
