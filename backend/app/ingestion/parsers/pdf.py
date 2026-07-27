"""PDF parsing via PyMuPDF (T-203, §10.1, FR-KBM-05).

PyMuPDF earns its place here for one reason the spec names explicitly: it yields **page
numbers**, and a page number is the FR-CIT-03 locator ("p. 14"). One block per page keeps
that mapping exact — a chunk can always name the page it came from, with no interpolation.

**Security posture (R-31, §8.12).** A malformed PDF attacking the parser is the most
realistic exploitation route into this pipeline, which is why R-31 requires PyMuPDF be
kept current (`pyproject.toml` floors it; bump it deliberately, do not let it drift) and
why parsing happens in the worker, isolated from the API process. The file has already
been sniffed at upload (`%PDF-` strictly at offset 0, defeating polyglots) and scanned by
ClamAV plus the R-32 structural checks (`/JavaScript`, `/OpenAction`, `/EmbeddedFile`)
before this module sees it. Nothing here renders, executes, or follows anything: text
extraction only, no `get_pixmap`, no link resolution, no external fetch.
"""

from __future__ import annotations

import pymupdf

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    DocumentTooComplexError,
    EncryptedDocumentError,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    page_locator,
    split_text,
)
from app.ingestion.parsers.text import is_blank, normalize

SUFFIX = ".pdf"


def parse(payload: bytes, *, limits: ParserSettings) -> ParsedDocument:
    """Extract per-page text from a PDF."""
    try:
        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception as exc:
        # PyMuPDF signals a damaged or non-PDF stream with FileDataError (a RuntimeError
        # subclass), but also raises ValueError/EmptyFileError depending on the defect —
        # a broad catch keeps a MuPDF internal from surfacing as a 500-equivalent job
        # crash instead of a clean FR-ING-01 FAILED.
        raise CorruptDocumentError(f"PDF could not be opened: {exc}") from exc

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
                raise CorruptDocumentError(
                    f"PDF page {page_number} could not be read: {exc}"
                ) from exc

            content = normalize(raw)
            if is_blank(content):
                continue
            budget.add(content)
            locator = page_locator(page_number)
            for part in split_text(content, limits.max_block_chars):
                blocks.append(ParsedBlock(text=part, locator=locator, order=len(blocks)))
    finally:
        document.close()

    if not blocks:
        raise NoExtractableTextError(
            "no text could be extracted from this PDF — it is most likely a scan or "
            "image-only export, which needs OCR"
        )

    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks), page_count=page_count)
