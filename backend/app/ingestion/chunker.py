"""Chunking, chunk identity, and the chunk-metadata contract (T-204, ruling R-35).

Turns the :class:`~app.ingestion.parsers.base.ParsedDocument` handed back by T-203 into
embeddable :class:`Chunk` records, and those into unsaved `document_chunks` rows. Pure
and deterministic: no clock, no RNG, no I/O, no session. T-205 embeds, T-207 persists.

**Chunks are cut within a `ParsedBlock` and never across one.** Two reasons, and the
second is the load-bearing one:

1. Locator fidelity. A chunk inherits its block's :class:`Locator` verbatim, so "p. 14"
   on the FR-CIT-03 hover card is a page the user can actually turn to. Text spanning two
   blocks would have no honest address.
2. Bounded re-embedding. The splitter is a left-to-right greedy scan, so an edit at offset
   *p* shifts every boundary after *p*. Block scoping stops that cascade at the block
   edge: a one-word fix on page 1 re-embeds page 1, not the document. Without it FR-ING-03
   would be incremental in name only.

FR-ING-03's ``embedding_fingerprint = SHA-256(chunk_text | embedding_model |
chunking_version | preprocessing_version)`` is assembled here.
:data:`CHUNKING_VERSION` identifies the *algorithm*; the string that actually enters the
formula is :func:`effective_chunking_version`, which folds in the sizing knobs — they are
§8.4 TBDs, so they will be tuned, and a tune moves chunk boundaries. Making the knobs part
of the version means FR-ING-03's "a changed chunking strategy forces re-embedding" holds by
construction instead of by someone remembering to bump a constant. (Its sibling
`PREPROCESSING_VERSION` is owned by the parsers per R-34(6); note that changing
``PARSER_MAX_BLOCK_CHARS`` is a *block-grouping* change and so requires bumping it.)

:func:`chunk_document` wraps the sync path in ``asyncio.to_thread`` for the T-207 worker.
As with the parsers, a thread cannot be cancelled: what bounds the work is
:class:`~app.config.ChunkerSettings` and the parser caps upstream, not the arq timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass

from app.config import EMBEDDING_MAX_INPUT_CHARS, ChunkerSettings, get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.models.document_chunk import DocumentChunk
from app.ingestion.parsers.base import (
    Extraction,
    Locator,
    LocatorKind,
    ParsedBlock,
    ParsedDocument,
)
from app.tokens import CHARS_PER_TOKEN, estimate_tokens

__all__ = [
    "CHUNKING_VERSION",
    "EMBEDDING_MAX_INPUT_CHARS",
    "Chunk",
    "ChunkedDocument",
    "build_chunk_rows",
    "chunk_document",
    "chunk_document_sync",
    "compute_chunk_hash",
    "compute_embedding_fingerprint",
    "effective_chunking_version",
    "estimate_token_count",
    "split_spans",
]

#: Identity of the splitting *algorithm*. Bump on any behavioural change — separator
#: hierarchy, overlap rule, tail handling. Sizing changes are captured automatically by
#: :func:`effective_chunking_version`, so this constant tracks code, not configuration.
CHUNKING_VERSION = "1"

#: The characters-per-token rule now lives in `app.tokens` (T-310), because the NFR-CAP-01
#: budget applies the same one and two definitions would let the number a user is shown
#: drift from the number that blocked them. Re-exported here so this module's existing
#: callers and tests are unaffected; the reasoning for it being an estimate — unchanged, and
#: still R-35(7)'s — is in that module's docstring.
_CHARS_PER_TOKEN = CHARS_PER_TOKEN

# Separator hierarchy, coarsest first. The boundary is always ``match.end()``, so the
# separator stays with the chunk that precedes it and no character is orphaned between
# two chunks. Note P0 ⊂ P1 ⊂ P3 and P2 ⊂ P3 — the ordering is the entire point.
_SEPARATORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n[ \t]*\n"),  # paragraph
    re.compile(r"\n"),  # line
    re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+"),  # sentence
    re.compile(r"\s+"),  # word
)

#: Index of the line separator in :data:`_SEPARATORS`. In a `rows` block the atomic unit
#: is the record, not the word — `csv.py` renders one record per line — so the splitter is
#: forbidden the finer separators there. Without this, a word-aligned overlap cheerfully
#: starts a chunk in the middle of ``Acme | EU | 12``.
_LINE_PRIORITY = 1


# --- identity -----------------------------------------------------------------


def effective_chunking_version(settings: ChunkerSettings) -> str:
    """The `chunking_version` that enters the FR-ING-03 fingerprint.

    Readable rather than a digest (``"1/2000/200/200/0.5"``) so a row explains its own
    re-embed: when a chunk comes back changed, the version string names the knob that
    moved.
    """
    return (
        f"{CHUNKING_VERSION}"
        f"/{settings.target_chars}"
        f"/{settings.overlap_chars}"
        f"/{settings.min_chars}"
        f"/{settings.boundary_floor_ratio:g}"
    )


def compute_chunk_hash(text: str) -> str:
    """Content identity of a chunk: SHA-256 of the stored text, hex (fits `CHAR(64)`).

    **Text only — never the position.** Folding `chunk_index` or the locator in would make
    inserting one paragraph at the top of a document change every hash below it, forcing a
    full re-embed. Avoiding exactly that is why FR-ING-03 exists.

    Consequence, ruled in R-35: a hash is *not* unique within a document. Repeated
    boilerplate ("This page intentionally left blank"), per-section disclaimers and
    duplicated CSV row groups all collide legitimately. T-205 may dedupe embedding *calls*
    by hash — an embedding is a pure function of text — but must never dedupe chunk
    *rows*; row identity is `chunk_index`.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_embedding_fingerprint(
    text: str,
    *,
    embedding_model: str,
    chunking_version: str,
    preprocessing_version: str,
) -> str:
    """FR-ING-03's fingerprint, assembled exactly as the spec writes it."""
    value = "|".join([text, embedding_model, chunking_version, preprocessing_version])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def estimate_token_count(text: str) -> int:
    """Approximate token count for `document_chunks.token_count`.

    Thin alias over :func:`app.tokens.estimate_tokens`, kept so this module's public surface
    and its tests are unchanged by T-310's promotion of the rule.
    """
    return estimate_tokens(text)


# --- splitting ----------------------------------------------------------------


def _last_boundary(text: str, pattern: re.Pattern[str], lo: int, limit: int) -> int | None:
    """Offset of the last ``pattern`` boundary in ``(lo, limit]``, or None.

    Scans the window with ``pos``/``endpos`` rather than slicing: at
    ``PARSER_MAX_EXTRACTED_CHARS`` a precomputed word-boundary list would be ~100 MB, and
    slicing would also hide the preceding character from the sentence pattern's lookbehind.
    """
    if lo >= limit:
        return None
    best: int | None = None
    for match in pattern.finditer(text, lo, limit):
        best = match.end()
    return best


def _overlap_start(
    text: str,
    start: int,
    end: int,
    settings: ChunkerSettings,
    separators: tuple[re.Pattern[str], ...],
) -> int:
    """Where the next chunk begins: at most ``overlap_chars`` back, snapped to a boundary.

    Overlap is a ceiling, not a guarantee. It never begins mid-word — nor mid-record, since
    ``separators`` is narrowed for line-structured blocks — and across a hard-sliced
    unbreakable run there is no boundary to snap to, so it is legitimately zero. The
    returned value is always in ``(start, end]``, which is what proves the splitter
    terminates.
    """
    if settings.overlap_chars <= 0:
        return end
    window = max(end - settings.overlap_chars, start + 1)
    for pattern in separators:
        match = pattern.search(text, window, end)
        if match is not None and start < match.end() < end:
            return match.end()
    return end


def _tighten(text: str, start: int, end: int) -> tuple[int, int]:
    """Trim whitespace from both edges of a span."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def split_spans(
    text: str,
    *,
    settings: ChunkerSettings,
    target_chars: int | None = None,
    finest_priority: int | None = None,
) -> list[tuple[int, int]]:
    """Split ``text`` into ``(start, end)`` spans — the algorithm core.

    Offsets rather than strings, so every chunk is literally ``text[start:end]``: the
    "chunk_text is verbatim" guarantee holds by construction rather than by discipline,
    and the metadata offsets come free.

    Boundary choice is the *last* boundary of the coarsest separator that clears a fill
    floor. The floor is what stops a blank line sitting just past the cursor from emitting
    a 250-character chunk when a sentence boundary was available at 1,990.

    ``finest_priority`` caps how fine a separator may be used, for blocks whose atomic unit
    is larger than a word (see :data:`_LINE_PRIORITY`). A trailing remainder shorter than
    ``min_chars`` is absorbed by the final chunk instead of being emitted alone, so the
    longest possible chunk is ``target + min_chars``.
    """
    target = settings.target_chars if target_chars is None else target_chars
    length = len(text)
    if length == 0:
        return []

    separators = _SEPARATORS if finest_priority is None else _SEPARATORS[: finest_priority + 1]
    min_chars = min(settings.min_chars, target)
    floor = max(min_chars, int(target * settings.boundary_floor_ratio))

    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < length:
        if length - cursor <= target:
            spans.append((cursor, length))
            break

        limit = cursor + target
        end: int | None = None
        for pattern in separators:
            end = _last_boundary(text, pattern, min(cursor + floor, limit), limit)
            if end is not None:
                break
        if end is None:
            # Nothing clears the floor — take the finest available break anywhere in the
            # window rather than slice through a word (or a record).
            end = _last_boundary(text, separators[-1], cursor, limit)
        if end is None:
            end = limit  # an unbreakable run: hard slice, bounded and deliberate

        if length - end < min_chars:  # absorb an orphan tail
            end = length

        spans.append((cursor, end))
        if end >= length:
            break
        cursor = _overlap_start(text, cursor, end, settings, separators)

    tightened = (_tighten(text, start, end) for start, end in spans)
    return [(start, end) for start, end in tightened if start < end]


def _row_header(block: ParsedBlock, *, target_chars: int) -> str:
    """The CSV header line to repeat on this block's later chunks, or ``""``.

    `csv.py` prepends the header to each *block*, and 50 rows comfortably exceed one
    chunk — so without this, chunks 2..n of a CSV block ship as bare ``Acme | EU | 12``
    with no column names: near-useless to embed and meaningless in a citation.

    Whether a header is present cannot be read off the text (the parser omits it when
    :func:`~app.ingestion.parsers.csv._looks_like_header` says the first record is data),
    but it can be *counted*: `_render` collapses each record onto exactly one line, so a
    block carries a header precisely when it has one more line than the row range covers.
    A `split_text` fragment fails that test and is skipped, which is the safe outcome.

    Repeating a header row keeps FR-CIT-03 honest in a way a synthetic heading would not —
    it is a real line of the user's own file, not text Corpus invented.
    """
    locator = block.locator
    if locator.kind is not LocatorKind.ROWS or locator.row_start is None:
        return ""
    if locator.row_end is None:
        return ""
    lines = block.text.split("\n")
    if len(lines) != locator.row_end - locator.row_start + 2:
        return ""
    return _usable_header(lines[0], target_chars=target_chars)


def _usable_header(header: str, *, target_chars: int) -> str:
    """``header`` unless it would crowd out the rows it labels, which is worse than none."""
    if not header or len(header) + 1 > target_chars // 2:
        return ""
    return header


def _repeated_header(block: ParsedBlock, *, target_chars: int) -> str:
    """The line to repeat on this block's chunks 2..n, or ``""``.

    Two producers, one rule (R-35(11), extended by R-88(6)). A **table** block declares its
    header — `pdf.py` guarantees it is line 0 verbatim — because a `page` locator cannot express
    the row range `_row_header` counts against, and because a table whose header cells are empty
    must not have its first data row promoted into one. A **CSV** block is still inferred, since
    its parser omits the header when the first record is data and only the line count can tell.
    """
    if block.header:
        return _usable_header(block.header, target_chars=target_chars)
    return _row_header(block, target_chars=target_chars)


# --- results ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """One embeddable unit. Pure data — no ids, no ORM, no session."""

    text: str  # → `document_chunks.chunk_text`, verbatim and never blank
    chunk_index: int  # 0-based, document-wide, contiguous
    chunk_hash: str
    embedding_fingerprint: str
    token_count: int
    locator: Locator  # inherited from the parent block, unmodified
    block_order: int  # `ParsedBlock.order` this came from
    block_chunk_index: int  # 0-based ordinal within that block
    char_start: int  # span into the parent block's text (block-relative)
    char_end: int
    #: R-88(7) provenance, inherited from the parent block. Deliberately **not** defaulted,
    #: unlike `ParsedBlock.extraction`: that default serves three formats which can only ever
    #: produce extracted text, whereas this has exactly one producer, and a default here would
    #: let a future call site silently label recognised text as `text` — the one error the
    #: marker exists to make impossible.
    extraction: Extraction


@dataclass(frozen=True, slots=True)
class ChunkedDocument:
    """The complete chunk set for one parse of one document version."""

    chunks: tuple[Chunk, ...]
    embedding_model: str
    chunking_version: str
    preprocessing_version: str

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def char_count(self) -> int:
        return sum(len(chunk.text) for chunk in self.chunks)

    def meta_for(self, chunk: Chunk) -> dict:
        """The `document_chunks.metadata` payload (R-35).

        A contract, not an implementation detail: T-206 projects `locator` straight into
        the FR-CIT-03 hover card. The three version strings live here — as the
        `document_chunk` model docstring promises — so an engineer can answer "why did this
        row re-embed?" from the row alone.

        `block_order` is load-bearing, not decorative: `split_text` gives every part of an
        over-long block the *same* `Locator` instance, so it is the only thing telling two
        chunks of page 14 apart. Offsets are **block-relative** — `ParsedDocument.text`
        joins blocks with a synthetic separator, so there is no honest document-absolute
        offset. `char_count` is the length of the stored text, which for a CSV chunk
        carrying a repeated header is the only truthful one.

        `extraction` is R-88(7)'s provenance marker (`text` | `ocr` | `table`), additive to the
        R-35 contract. It is what makes an OCR-quality complaint diagnosable at all: without
        it, "the citation is garbled" cannot be told from "the document is garbled". It is not
        a fingerprint input, so adding it re-embeds nothing.

        Absent by design: `chunk_hash`, `token_count`, `tenant_id`,
        `knowledge_base_id`, `document_id`, `document_version` — every one is a first-class
        column, and duplicating a column into JSONB creates a second source of truth.
        """
        return {
            "locator": chunk.locator.as_metadata(),
            "block_order": chunk.block_order,
            "block_chunk_index": chunk.block_chunk_index,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "char_count": len(chunk.text),
            "embedding_model": self.embedding_model,
            "chunking_version": self.chunking_version,
            "preprocessing_version": self.preprocessing_version,
            "extraction": chunk.extraction.value,
        }


def chunk_document_sync(
    parsed: ParsedDocument,
    *,
    embedding_model: str | None = None,
    settings: ChunkerSettings | None = None,
) -> ChunkedDocument:
    """Chunk a parsed document. Deterministic: same input, byte-identical output."""
    app_settings = get_settings()
    settings = settings or app_settings.chunker
    embedding_model = embedding_model or app_settings.openai.embedding_model
    chunking_version = effective_chunking_version(settings)

    chunks: list[Chunk] = []
    for block in parsed.blocks:
        # A table row is atomic exactly as a CSV record is — both render one row per line, so
        # the finer separators would let a word-aligned overlap open a chunk in the middle of
        # `EU | 12 | 15`. R-35(4) states the rule for `rows` blocks; FR-ING-08 produces the
        # second kind of line-structured block, and leaving it out is invisible until a citation
        # quotes half a row.
        rows_block = block.locator.kind is LocatorKind.ROWS or block.extraction is Extraction.TABLE
        header = _repeated_header(block, target_chars=settings.target_chars)
        target = settings.target_chars - (len(header) + 1 if header else 0)

        spans = split_spans(
            block.text,
            settings=settings,
            target_chars=target,
            finest_priority=_LINE_PRIORITY if rows_block else None,
        )
        for block_chunk_index, (start, end) in enumerate(spans):
            body = block.text[start:end]
            # Chunk 0 already opens with the header — the parser put it there.
            text = f"{header}\n{body}" if header and block_chunk_index else body
            if not text.strip():
                # Unreachable given the parser contract, but an empty input is a hard 400
                # from the embeddings endpoint, and T-205 would report it as a mystery.
                continue
            chunks.append(
                Chunk(
                    text=text,
                    chunk_index=len(chunks),
                    chunk_hash=compute_chunk_hash(text),
                    embedding_fingerprint=compute_embedding_fingerprint(
                        text,
                        embedding_model=embedding_model,
                        chunking_version=chunking_version,
                        preprocessing_version=parsed.preprocessing_version,
                    ),
                    token_count=estimate_token_count(text),
                    locator=block.locator,
                    block_order=block.order,
                    block_chunk_index=block_chunk_index,
                    char_start=start,
                    char_end=end,
                    extraction=block.extraction,
                )
            )

    return ChunkedDocument(
        chunks=tuple(chunks),
        embedding_model=embedding_model,
        chunking_version=chunking_version,
        preprocessing_version=parsed.preprocessing_version,
    )


async def chunk_document(
    parsed: ParsedDocument,
    *,
    embedding_model: str | None = None,
    settings: ChunkerSettings | None = None,
) -> ChunkedDocument:
    """Async facade over :func:`chunk_document_sync` for the ingestion worker (T-207)."""
    return await asyncio.to_thread(
        chunk_document_sync, parsed, embedding_model=embedding_model, settings=settings
    )


def build_chunk_rows(
    chunked: ChunkedDocument,
    *,
    document_id: uuid.UUID,
    document_version: int,
    knowledge_base_id: uuid.UUID,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,  # single-org (OI-21)
) -> list[DocumentChunk]:
    """Build unsaved `document_chunks` rows carrying the FR-RET-04 access scope.

    Ids are explicit parameters rather than a `Document`, because T-205 builds rows for the
    *added* subset of a re-ingest and stamps its own version. `embedding` is left ``None``
    for T-205 to fill. `tenant_id` is set in Python rather than left to the server default,
    so the value is readable in-session before the flush.
    """
    return [
        DocumentChunk(
            document_id=document_id,
            document_version=document_version,
            chunk_index=chunk.chunk_index,
            chunk_hash=chunk.chunk_hash,
            embedding_fingerprint=chunk.embedding_fingerprint,
            token_count=chunk.token_count,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            chunk_text=chunk.text,
            meta=chunked.meta_for(chunk),
        )
        for chunk in chunked.chunks
    ]
