"""PDF parsing via PyMuPDF (T-203, §10.1, FR-KBM-05; recognition added T-218, R-88 §8.78).

PyMuPDF earns its place here for one reason the spec names explicitly: it yields **page
numbers**, and a page number is the FR-CIT-03 locator ("p. 14"). One block per page keeps
that mapping exact — a chunk can always name the page it came from, with no interpolation.

**Each page decides for itself whether to recognise (R-88(2)).** A page whose text layer
yields content is never rasterised and never compared against a recognised alternative
(R-88(3)) — its characters are ground truth and OCR of the same page is a lossy
re-derivation. A page that yields nothing is rendered and sent to the recognition sidecar;
so are the qualifying embedded rasters of a *textual* page (R-88(4)), which the
document-level branch this replaces could never see, because one extractable page anywhere
suppressed it forever. Recognition **fails open** (R-88(9)): a sidecar that is down, slow or
erroring degrades this parser to exactly the behaviour that shipped before Rev 0.55. The
document-level rule still fails closed — `NoExtractableTextError` is now evaluated *after*
recognition, which is what preserves R-34's guarantee that no zero-chunk document goes
`ACTIVE` and lists as ready while retrieval can never answer from it.

**Security posture (R-31 §8.12, NFR-SEC-09 §8.78).** A malformed PDF attacking the parser is
the most realistic exploitation route into this pipeline, which is why R-31 requires PyMuPDF
be kept current (`pyproject.toml` floors it; bump it deliberately, do not let it drift) and
why parsing happens in the worker, isolated from the API process. The file has already been
sniffed at upload (`%PDF-` strictly at offset 0, defeating polyglots) and scanned by ClamAV
plus the R-32 structural checks (`/JavaScript`, `/OpenAction`, `/EmbeddedFile`) before this
module sees it.

This module now **renders**, which the pre-Rev-0.55 docstring promised it never would, so the
claim is restated rather than quietly dropped (R-88(10), R-56). Rasterisation happens here
and only here, in the **worker** — R-31's placement rule is unchanged and is now load-bearing
twice over. The raster then crosses a local socket to a sidecar holding the image decoders
and the recognition engine, so a memory-safety defect in either cannot reach this process's
database session or its object-storage credentials. Nothing leaves the deployment: the one
outbound destination is composed entirely from `OcrSettings`. Still no link resolution and no
external fetch. Rendering is itself bounded — see `recognition.effective_dpi`, because
`get_pixmap` allocates before anything can weigh the result.

**Bumping PyMuPDF is now a re-embed decision as well as a security one.** A MuPDF rendering
change alters the raster, hence the recognised text, hence `chunk_text`, hence every OCR'd
chunk's `embedding_fingerprint` — with no version string moving to explain it (R-88(1)).
"""

from __future__ import annotations

import pymupdf
import structlog

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    DocumentTooComplexError,
    EncryptedDocumentError,
    Extraction,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    page_locator,
    split_text,
)
from app.ingestion.parsers.recognition import (
    RecognitionBudget,
    accepted_text,
    has_renderable_content,
    qualifying_images,
    render_png,
)
from app.ingestion.parsers.text import is_blank, normalize
from app.services.ocr import OcrClient, get_ocr_client

log = structlog.get_logger(__name__)

SUFFIX = ".pdf"

#: The whole page, as opposed to a clip within it. Named so the region loop reads the same
#: for a scanned page and for a figure on a textual one.
_WHOLE_PAGE: pymupdf.Rect | None = None


def parse(
    payload: bytes, *, limits: ParserSettings, recognizer: OcrClient | None = None
) -> ParsedDocument:
    """Extract per-page text from a PDF, recognising what has no text layer.

    ``recognizer`` exists so tests can substitute a double without a socket; it is typed as
    the concrete client rather than a Protocol because R-88(1) refuses a pluggable recogniser
    seam outright — the only alternative vendor class is the hosted-vision one it excludes on
    determinism grounds, and a Protocol here would advertise a substitution that must not be
    made. Passing one does **not** enable recognition: `PARSER_OCR_ENABLED` is the single
    gate, so a double cannot smuggle the feature past its own off switch.
    """
    try:
        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception as exc:
        # PyMuPDF signals a damaged or non-PDF stream with FileDataError (a RuntimeError
        # subclass), but also raises ValueError/EmptyFileError depending on the defect —
        # a broad catch keeps a MuPDF internal from surfacing as a 500-equivalent job
        # crash instead of a clean FR-ING-01 FAILED.
        raise CorruptDocumentError(f"PDF could not be opened: {exc}") from exc

    recognising = limits.ocr_enabled
    client = recognizer
    passes = RecognitionBudget.for_(limits)
    degraded: str | None = None

    try:
        if document.needs_pass:
            # Corpus has no password prompt and stores no credentials for documents;
            # an encrypted PDF is terminal, not retryable.
            raise EncryptedDocumentError("PDF is password-protected and cannot be read")

        page_count = document.page_count
        if page_count > limits.max_pages:
            raise DocumentTooComplexError(
                f"PDF has {page_count:,} pages — the limit is {limits.max_pages:,}"
            )

        budget = CharBudget(limits.max_extracted_chars)
        blocks: list[ParsedBlock] = []
        for index in range(page_count):
            page_number = index + 1
            try:
                page = document.load_page(index)
                # `sort=True` orders text by position rather than by the order the
                # content stream happens to emit it, so multi-column and reflowed pages
                # read top-to-bottom instead of interleaving columns mid-sentence.
                raw = page.get_text("text", sort=True)
            except Exception as exc:
                # Extraction still fails **closed**. Only recognition fails open, and
                # blurring the two into one broad handler is how R-88(9) would be lost.
                raise CorruptDocumentError(
                    f"PDF page {page_number} could not be read: {exc}"
                ) from exc

            content = normalize(raw)
            locator = page_locator(page_number)

            if not is_blank(content):
                budget.add(content)
                for part in split_text(content, limits.max_block_chars):
                    blocks.append(
                        ParsedBlock(
                            text=part,
                            locator=locator,
                            order=len(blocks),
                            extraction=Extraction.TEXT,
                        )
                    )
                # R-88(4): the page keeps its own characters; only its rasters are candidates.
                regions = qualifying_images(page, limits=limits) if recognising else []
            elif recognising and has_renderable_content(page):
                # R-88(2): a page with no text layer decides for itself, whatever the rest of
                # the document managed. A blank separator page reaches neither branch, so it
                # costs no raster, no round trip and no ceiling spend.
                regions = [_WHOLE_PAGE]
            else:
                continue

            for region in regions:
                if not passes.allows():
                    # R-88(11). Stop recognising; do **not** fail the document — a partially
                    # searchable scan is more useful than a rejected one, and R-34(5)'s
                    # caps-reject-never-truncate rule governs input size limits, not how much
                    # optional enrichment ran.
                    break
                try:
                    if client is None:
                        client = get_ocr_client()
                    image = render_png(page, clip=region, limits=limits)
                    result = client.recognize(image)
                except Exception as exc:  # noqa: BLE001 — R-88(9): recognition fails open.
                    # Deliberately broad: a render fault, a dead sidecar and a malformed
                    # response are all "recognition did not happen", and none of them may
                    # fail a document that has a perfectly good text layer. The exception
                    # *class* is kept for one diagnostic line; the page's bytes are not.
                    degraded = degraded or type(exc).__name__
                    passes.spend()
                    continue
                passes.spend()

                text = accepted_text(result, floor=limits.ocr_min_confidence)
                if text is None:
                    # Nothing recognised, or below the R-88(8) floor. Either way the page
                    # contributes no block, exactly as an empty one does.
                    continue
                recognised = normalize(text)
                if is_blank(recognised):
                    continue
                # Charged against the same ceiling as extracted text: without this a long
                # scan would bypass `PARSER_MAX_EXTRACTED_CHARS` entirely.
                budget.add(recognised)
                for part in split_text(recognised, limits.max_block_chars):
                    blocks.append(
                        ParsedBlock(
                            text=part,
                            locator=locator,
                            order=len(blocks),
                            extraction=Extraction.OCR,
                        )
                    )
    finally:
        # Nothing derived from `document` may outlive this block: a `Pixmap` is a handle into
        # its MuPDF context, so using one after the close is a use-after-free in C rather
        # than a Python error. `render_png` returns `bytes` precisely so that cannot happen.
        document.close()

    if degraded or passes.exhausted_by:
        # Ids, counts and an exception class only — never a page's text and never its raster
        # (R-43(5), and the content-leak T-217 closed on the other side of this socket).
        log.warning(
            "pdf.recognition_degraded",
            reason=degraded,
            exhausted_by=passes.exhausted_by,
            recognised_passes=passes.spent,
            pages=page_count,
        )

    if not blocks:
        raise NoExtractableTextError(
            "no text could be extracted from this PDF — it is most likely a scan or "
            "image-only export, and OCR produced nothing usable either"
            if recognising
            else "no text could be extracted from this PDF — it is most likely a scan or "
            "image-only export, which needs OCR"
        )

    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks), page_count=page_count)
