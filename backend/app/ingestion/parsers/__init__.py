"""Document parsers — dispatch seam for the ingestion worker (T-203, FR-KBM-05).

One entry point per calling style:

* :func:`parse_document_sync` — pure, blocking, no I/O. What the tests exercise.
* :func:`parse_document` — the async facade the T-207 worker calls; identical semantics,
  off the event loop.

**Parse in the worker, never in the API process** (R-31, §8.12 and the T-203 task line).
Extraction is CPU-bound C code operating on hostile input, and the upload endpoint has to
stay responsive enough to return its `202`. A test in `tests/test_parsers.py` asserts that
nothing under `app/api/` imports this package, so the rule fails loudly rather than
eroding.

**Cancellation caveat for T-207.** :func:`asyncio.to_thread` cannot be cancelled — a
timeout around it returns control to the caller while the thread keeps running to
completion. The per-format limits in :class:`~app.config.ParserSettings` are what
actually bound the work; the arq job timeout is a backstop for the worker, not a way to
reclaim the thread. Anything added here must stay bounded by a limit rather than by a
clock.
"""

from __future__ import annotations

import asyncio

from app.config import ParserSettings, get_settings
from app.ingestion.parsers import csv as csv_parser
from app.ingestion.parsers import docx as docx_parser
from app.ingestion.parsers import markdown as markdown_parser
from app.ingestion.parsers import pdf as pdf_parser
from app.ingestion.parsers.base import (
    PREPROCESSING_VERSION,
    CorruptDocumentError,
    DocumentTooComplexError,
    EncryptedDocumentError,
    Locator,
    LocatorKind,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    ParseError,
    Parser,
    UnsupportedDocumentError,
)
from app.ingestion.parsers.text import normalize
from app.security.content_validation import normalized_suffix

#: Suffix → parser. The keys are exactly the FR-ERR-03 formats that
#: `app.security.content_validation` admits (R-11); a suffix reaching this module that is
#: not here means the upload gate was bypassed, which is why the miss raises rather than
#: guessing a parser from content.
PARSERS: dict[str, Parser] = {
    pdf_parser.SUFFIX: pdf_parser.parse,
    docx_parser.SUFFIX: docx_parser.parse,
    csv_parser.SUFFIX: csv_parser.parse,
    markdown_parser.SUFFIX: markdown_parser.parse,
}


def parse_document_sync(
    payload: bytes, *, filename: str, limits: ParserSettings | None = None
) -> ParsedDocument:
    """Parse ``payload`` according to ``filename``'s extension.

    The extension selects the parser only; it is never the security boundary. T-202's
    magic-byte sniffing already established that the content matches the declared
    extension (and rejected any file where it did not), so by the time bytes reach here
    "trust the extension" means "trust the sniffer's verdict".
    """
    suffix = normalized_suffix(filename)
    parser = PARSERS.get(suffix)
    if parser is None:
        raise UnsupportedDocumentError(f"no parser for {suffix or filename!r}")
    if not payload:
        raise CorruptDocumentError("file is empty")
    return parser(payload, limits=limits or get_settings().parser)


async def parse_document(
    payload: bytes, *, filename: str, limits: ParserSettings | None = None
) -> ParsedDocument:
    """Async wrapper over :func:`parse_document_sync` for the ingestion worker."""
    return await asyncio.to_thread(parse_document_sync, payload, filename=filename, limits=limits)


__all__ = [
    "PARSERS",
    "PREPROCESSING_VERSION",
    "CorruptDocumentError",
    "DocumentTooComplexError",
    "EncryptedDocumentError",
    "Locator",
    "LocatorKind",
    "NoExtractableTextError",
    "ParseError",
    "ParsedBlock",
    "ParsedDocument",
    "Parser",
    "UnsupportedDocumentError",
    "normalize",
    "parse_document",
    "parse_document_sync",
]
