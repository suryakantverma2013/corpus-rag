"""Chunker tests (T-204, R-35).

Self-contained and DB-free: most tests build a :class:`ParsedDocument` by hand, so they
exercise the splitter without paying for a parse. The few that need real bytes generate
them in-process exactly as `test_parsers.py` does.

Sizing is exercised by shrinking :class:`ChunkerSettings` rather than by building 2,000-
character strings — ``target_chars=40`` proves everything ``target_chars=2000`` would, and
keeps the assertions readable.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from app.config import EMBEDDING_MAX_INPUT_CHARS, ChunkerSettings
from app.ingestion.chunker import (
    CHUNKING_VERSION,
    build_chunk_rows,
    chunk_document,
    chunk_document_sync,
    effective_chunking_version,
    estimate_token_count,
    split_spans,
)
from app.ingestion.parsers import parse_document_sync
from app.ingestion.parsers.base import (
    PREPROCESSING_VERSION,
    Extraction,
    LocatorKind,
    ParsedBlock,
    ParsedDocument,
    page_locator,
    rows_locator,
    section_locator,
)

MODEL = "text-embedding-3-large"

# --- fixtures -----------------------------------------------------------------


def _settings(**overrides) -> ChunkerSettings:
    base = {"target_chars": 100, "overlap_chars": 10, "min_chars": 10}
    return ChunkerSettings(**(base | overrides))


def _parsed(*texts: str, locators=None, extraction=Extraction.TEXT) -> ParsedDocument:
    locators = locators or [page_locator(index + 1) for index in range(len(texts))]
    blocks = tuple(
        ParsedBlock(text=text, locator=locator, order=order, extraction=extraction)
        for order, (text, locator) in enumerate(zip(texts, locators, strict=True))
    )
    return ParsedDocument(suffix=".pdf", blocks=blocks, page_count=len(blocks))


def _chunk(*texts: str, settings: ChunkerSettings | None = None, **kwargs):
    return chunk_document_sync(
        _parsed(*texts, **kwargs), embedding_model=MODEL, settings=settings or _settings()
    )


def _sentences(count: int, *, word: str = "alpha") -> str:
    return " ".join(f"{word} beta gamma delta epsilon number {index}." for index in range(count))


# --- splitting ----------------------------------------------------------------


def test_a_short_block_becomes_one_chunk() -> None:
    chunked = _chunk("A single short paragraph.")
    assert chunked.chunk_count == 1
    assert chunked.chunks[0].text == "A single short paragraph."


def test_no_chunk_exceeds_the_target_plus_the_orphan_threshold() -> None:
    settings = _settings(target_chars=100, min_chars=10)
    chunked = _chunk(_sentences(60), settings=settings)
    assert chunked.chunk_count > 1
    assert max(len(chunk.text) for chunk in chunked.chunks) <= 100 + 10


def test_split_prefers_paragraph_boundaries() -> None:
    text = "\n\n".join(["para one is here"] * 12)
    spans = split_spans(text, settings=_settings(target_chars=80, overlap_chars=0))
    assert len(spans) > 1
    for _, end in spans[:-1]:
        # Spans are tightened, so the paragraph break sits immediately *after* the end.
        assert text[end : end + 2] == "\n\n"


@pytest.mark.parametrize(
    ("text", "expected_separator"),
    [
        ("\n\n".join(["paragraph text here"] * 12), "\n\n"),
        ("\n".join(["single line of text"] * 12), "\n"),
        (_sentences(12), ". "),
        ("word " * 60, " "),
    ],
)
def test_split_falls_back_through_the_separator_hierarchy(text, expected_separator) -> None:
    spans = split_spans(text, settings=_settings(target_chars=80, overlap_chars=0))
    assert len(spans) > 1
    # Every boundary lands immediately after an instance of the coarsest available class.
    boundary = spans[0][1]
    assert (
        expected_separator.strip() in text[max(0, boundary - 3) : boundary + 2]
        or text[boundary - 1].isspace()
    )


def test_a_paragraph_boundary_below_the_fill_floor_is_ignored() -> None:
    # A blank line at offset ~20 with the floor at 50: taking it would emit a 20-char
    # chunk when the splitter could have filled 100.
    text = "tiny intro para.\n\n" + _sentences(20)
    spans = split_spans(
        text, settings=_settings(target_chars=100, overlap_chars=0, boundary_floor_ratio=0.5)
    )
    assert spans[0][1] - spans[0][0] >= 50


def test_an_unbreakable_run_is_hard_sliced_at_the_target() -> None:
    text = "x" * 250
    spans = split_spans(text, settings=_settings(target_chars=100, overlap_chars=10, min_chars=10))
    assert len(spans) == 3
    assert spans[0] == (0, 100)
    # No boundary exists to snap back to, so overlap is legitimately zero.
    assert spans[1][0] == 100


def test_every_character_survives_chunking() -> None:
    text = _sentences(40)
    spans = split_spans(text, settings=_settings(target_chars=100, overlap_chars=0))
    rejoined = "".join(text[start:end] for start, end in spans)
    assert rejoined.replace(" ", "") == text.replace(" ", "")


def test_no_chunk_is_blank_or_whitespace_only() -> None:
    chunked = _chunk("first\n\n\n\nsecond\n\n\n\nthird" + "\n\n" + _sentences(30))
    assert all(chunk.text.strip() for chunk in chunked.chunks)


def test_an_orphan_tail_is_absorbed_by_the_previous_chunk() -> None:
    # 105 characters against a 100 target: the 5-char remainder must not stand alone.
    text = "word " * 21
    spans = split_spans(text, settings=_settings(target_chars=100, overlap_chars=0, min_chars=20))
    assert len(spans) == 1


def test_overlap_repeats_the_tail_of_the_previous_chunk() -> None:
    text = _sentences(40)
    settings = _settings(target_chars=100, overlap_chars=30)
    spans = split_spans(text, settings=settings)
    assert spans[1][0] < spans[0][1], "the second chunk must start before the first one ends"
    overlap = text[spans[1][0] : spans[0][1]]
    assert overlap and overlap in text[spans[0][0] : spans[0][1]]


def test_overlap_never_starts_mid_word() -> None:
    text = _sentences(40)
    spans = split_spans(text, settings=_settings(target_chars=100, overlap_chars=30))
    for start, _ in spans[1:]:
        assert start == 0 or text[start - 1].isspace()


def test_overlap_never_crosses_a_block_boundary() -> None:
    chunked = _chunk(_sentences(30), _sentences(30), settings=_settings(overlap_chars=30))
    first_block = [chunk for chunk in chunked.chunks if chunk.block_order == 0]
    second_block = [chunk for chunk in chunked.chunks if chunk.block_order == 1]
    assert first_block and second_block
    # The first chunk of block 1 starts at its own block's offset 0 — no bleed from block 0.
    assert second_block[0].char_start == 0


def test_chunking_the_same_input_twice_is_identical() -> None:
    text = _sentences(50)
    first, second = _chunk(text), _chunk(text)
    assert [chunk.chunk_hash for chunk in first.chunks] == [
        chunk.chunk_hash for chunk in second.chunks
    ]
    assert [chunk.text for chunk in first.chunks] == [chunk.text for chunk in second.chunks]


def test_editing_one_block_leaves_the_other_blocks_chunks_unchanged() -> None:
    """The FR-ING-03 property: a localised edit must not re-embed the document."""
    untouched_a, untouched_b = _sentences(20, word="alpha"), _sentences(20, word="omega")
    before = _chunk(untouched_a, "original middle text.", untouched_b)
    after = _chunk(untouched_a, "REVISED middle text entirely.", untouched_b)

    def hashes(chunked, block_order):
        return [c.chunk_hash for c in chunked.chunks if c.block_order == block_order]

    assert hashes(before, 0) == hashes(after, 0)
    assert hashes(before, 2) == hashes(after, 2)
    assert hashes(before, 1) != hashes(after, 1)


def test_overlap_at_or_above_the_target_is_rejected() -> None:
    # Not a poor setting — the splitter's cursor would stop advancing.
    with pytest.raises(ValidationError):
        ChunkerSettings(target_chars=100, overlap_chars=100)


def test_settings_reject_a_target_above_the_embedding_input_ceiling() -> None:
    with pytest.raises(ValidationError):
        ChunkerSettings(target_chars=EMBEDDING_MAX_INPUT_CHARS, min_chars=1)


# --- locators & metadata ------------------------------------------------------


def test_every_chunk_inherits_its_blocks_locator() -> None:
    chunked = _chunk(_sentences(40), _sentences(40))
    for chunk in chunked.chunks:
        assert chunk.locator.kind is LocatorKind.PAGE
        assert chunk.locator.page == chunk.block_order + 1


def test_chunks_from_split_blocks_are_distinguished_by_block_order() -> None:
    # `split_text` hands both halves of an over-long page the *same* Locator instance,
    # so block_order is the only thing telling their chunks apart.
    shared = page_locator(14)
    parsed = ParsedDocument(
        suffix=".pdf",
        blocks=(
            ParsedBlock(text="first half of page 14", locator=shared, order=0),
            ParsedBlock(text="second half of page 14", locator=shared, order=1),
        ),
        page_count=14,
    )
    chunked = chunk_document_sync(parsed, embedding_model=MODEL, settings=_settings())
    assert {chunk.locator.page for chunk in chunked.chunks} == {14}
    assert [chunk.block_order for chunk in chunked.chunks] == [0, 1]


def test_chunk_indexes_are_contiguous_across_the_document() -> None:
    chunked = _chunk(_sentences(30), _sentences(30), _sentences(30))
    assert [chunk.chunk_index for chunk in chunked.chunks] == list(range(chunked.chunk_count))


def test_chunk_metadata_shape() -> None:
    chunked = _chunk("A short single-chunk page.")
    chunk = chunked.chunks[0]
    assert chunked.meta_for(chunk) == {
        "locator": {"kind": "page", "label": "p. 1", "page": 1},
        "block_order": 0,
        "block_chunk_index": 0,
        "char_start": 0,
        "char_end": 26,
        "char_count": 26,
        "embedding_model": MODEL,
        "chunking_version": effective_chunking_version(_settings()),
        "preprocessing_version": PREPROCESSING_VERSION,
        "extraction": "text",
    }


def test_a_recognised_block_is_marked_ocr_in_the_chunk_metadata() -> None:
    """R-88(7): provenance travels block -> chunk -> `document_chunks.metadata`.

    Without it "the citation is garbled" cannot be told from "the document is garbled", which
    is the whole reason the marker exists. Asserted on a *split* block as well, because
    `split_text` hands every part one shared `Locator` — a marker put there instead of on the
    block would be indistinguishable from this until a page overflowed.
    """
    chunked = _chunk(_sentences(30), extraction=Extraction.OCR)
    assert len(chunked.chunks) > 1
    assert {chunked.meta_for(chunk)["extraction"] for chunk in chunked.chunks} == {"ocr"}


def test_the_marker_does_not_reach_the_fingerprint() -> None:
    """Metadata is not a fingerprint input, and that is what makes enabling OCR additive.

    If it were, turning recognition on would invalidate every existing chunk in the corpus —
    a fleet-wide re-embed disguised as a feature flag. The one legitimate bump is T-220's
    `PREPROCESSING_VERSION` change, which is deliberate and driven.
    """
    text = "A short single-chunk page."
    as_text = _chunk(text, extraction=Extraction.TEXT).chunks[0]
    as_ocr = _chunk(text, extraction=Extraction.OCR).chunks[0]
    assert as_text.embedding_fingerprint == as_ocr.embedding_fingerprint
    assert as_text.chunk_hash == as_ocr.chunk_hash


def test_section_locators_survive_into_the_metadata() -> None:
    locator = section_locator(("Guide", "Setup"), 2, fallback_label="paragraph 1")
    chunked = _chunk(_sentences(30), locators=[locator])
    meta = chunked.meta_for(chunked.chunks[0])["locator"]
    assert meta["kind"] == "section"
    assert meta["section_path"] == ["Guide", "Setup"]


# --- CSV row blocks -----------------------------------------------------------


def _csv_block(rows: int, *, header: bool = True) -> ParsedDocument:
    lines = [f"row{index} | value | {index}" for index in range(1, rows + 1)]
    if header:
        lines.insert(0, "name | label | count")
    block = ParsedBlock(text="\n".join(lines), locator=rows_locator(1, rows), order=0)
    return ParsedDocument(suffix=".csv", blocks=(block,))


def test_csv_chunks_break_on_row_boundaries() -> None:
    chunked = chunk_document_sync(
        _csv_block(30), embedding_model=MODEL, settings=_settings(target_chars=120)
    )
    assert chunked.chunk_count > 1
    for chunk in chunked.chunks:
        for line in chunk.text.split("\n"):
            assert line.count("|") == 2, "a record was bisected"


def test_csv_chunks_repeat_the_header_row() -> None:
    chunked = chunk_document_sync(
        _csv_block(30), embedding_model=MODEL, settings=_settings(target_chars=120)
    )
    assert chunked.chunk_count > 1
    for chunk in chunked.chunks:
        assert chunk.text.startswith("name | label | count")


def test_a_headerless_csv_block_gets_no_repeated_first_row() -> None:
    # The parser omits the header when the first record looks like data; repeating a data
    # row would present it as column names and duplicate it into every citation.
    chunked = chunk_document_sync(
        _csv_block(30, header=False), embedding_model=MODEL, settings=_settings(target_chars=120)
    )
    assert chunked.chunk_count > 1
    assert not chunked.chunks[1].text.startswith("row1 |")


# --- hash & fingerprint -------------------------------------------------------


def test_chunk_hash_is_sha256_of_the_chunk_text() -> None:
    chunked = _chunk("Exactly one chunk of text.")
    chunk = chunked.chunks[0]
    assert chunk.chunk_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()


def test_identical_text_at_two_positions_hashes_identically() -> None:
    # Repeated boilerplate is real (headers, disclaimers). R-35: T-205 may dedupe embedding
    # calls by hash, but never chunk rows — row identity is chunk_index.
    boilerplate = "This page intentionally left blank."
    chunked = _chunk(boilerplate, boilerplate)
    assert chunked.chunk_count == 2
    assert chunked.chunks[0].chunk_hash == chunked.chunks[1].chunk_hash
    assert chunked.chunks[0].chunk_index != chunked.chunks[1].chunk_index


def test_fingerprint_matches_the_fr_ing_03_formula() -> None:
    chunked = _chunk("One chunk.")
    chunk = chunked.chunks[0]
    expected = hashlib.sha256(
        "|".join(
            [chunk.text, MODEL, effective_chunking_version(_settings()), PREPROCESSING_VERSION]
        ).encode("utf-8")
    ).hexdigest()
    assert chunk.embedding_fingerprint == expected


def test_fingerprint_changes_when_the_embedding_model_changes() -> None:
    parsed = _parsed("One chunk.")
    large = chunk_document_sync(parsed, embedding_model=MODEL, settings=_settings())
    small = chunk_document_sync(
        parsed, embedding_model="text-embedding-3-small", settings=_settings()
    )
    assert large.chunks[0].chunk_hash == small.chunks[0].chunk_hash
    assert large.chunks[0].embedding_fingerprint != small.chunks[0].embedding_fingerprint


def test_fingerprint_changes_when_the_preprocessing_version_changes() -> None:
    block = ParsedBlock(text="One chunk.", locator=page_locator(1), order=0)
    first = chunk_document_sync(
        ParsedDocument(suffix=".pdf", blocks=(block,), preprocessing_version="1"),
        embedding_model=MODEL,
        settings=_settings(),
    )
    second = chunk_document_sync(
        ParsedDocument(suffix=".pdf", blocks=(block,), preprocessing_version="2"),
        embedding_model=MODEL,
        settings=_settings(),
    )
    assert first.chunks[0].embedding_fingerprint != second.chunks[0].embedding_fingerprint


def test_fingerprint_changes_when_a_sizing_knob_changes() -> None:
    """The composite `chunking_version` is what makes a knob tune re-embed."""
    parsed = _parsed("One short chunk.")
    first = chunk_document_sync(parsed, embedding_model=MODEL, settings=_settings())
    second = chunk_document_sync(
        parsed, embedding_model=MODEL, settings=_settings(overlap_chars=20)
    )
    assert first.chunks[0].chunk_hash == second.chunks[0].chunk_hash
    assert first.chunks[0].embedding_fingerprint != second.chunks[0].embedding_fingerprint


def test_effective_chunking_version_names_the_knobs() -> None:
    version = effective_chunking_version(ChunkerSettings())
    assert version == f"{CHUNKING_VERSION}/2000/200/200/0.5"


def test_reparsing_unchanged_bytes_reproduces_every_hash() -> None:
    document = pymupdf.open()
    for index in range(3):
        document.new_page().insert_text((72, 100), f"Page {index + 1} content")
    payload = document.tobytes()
    document.close()

    def hashes() -> list[str]:
        parsed = parse_document_sync(payload, filename="handbook.pdf")
        chunked = chunk_document_sync(parsed, embedding_model=MODEL, settings=_settings())
        return [chunk.embedding_fingerprint for chunk in chunked.chunks]

    assert hashes() == hashes()


# --- rows ---------------------------------------------------------------------


def test_rows_carry_document_and_knowledge_base_scope() -> None:
    document_id, kb_id = uuid.uuid4(), uuid.uuid4()
    chunked = _chunk(_sentences(30))
    rows = build_chunk_rows(
        chunked, document_id=document_id, document_version=3, knowledge_base_id=kb_id
    )
    assert len(rows) == chunked.chunk_count
    assert {row.document_id for row in rows} == {document_id}
    assert {row.knowledge_base_id for row in rows} == {kb_id}
    assert {row.document_version for row in rows} == {3}


def test_rows_are_active_and_unembedded() -> None:
    rows = build_chunk_rows(
        _chunk("One chunk."),
        document_id=uuid.uuid4(),
        document_version=1,
        knowledge_base_id=uuid.uuid4(),
    )
    assert rows[0].embedding is None  # T-205 fills it
    assert rows[0].token_count == estimate_token_count(rows[0].chunk_text)


def test_rows_default_to_the_placeholder_tenant() -> None:
    rows = build_chunk_rows(
        _chunk("One chunk."),
        document_id=uuid.uuid4(),
        document_version=1,
        knowledge_base_id=uuid.uuid4(),
    )
    # single-org sentinel (OI-21, settled by R-62(4))
    assert rows[0].tenant_id == uuid.UUID("00000000-0000-0000-0000-000000000000")


def test_row_metadata_matches_the_chunk_metadata() -> None:
    chunked = _chunk(_sentences(30))
    rows = build_chunk_rows(
        chunked, document_id=uuid.uuid4(), document_version=1, knowledge_base_id=uuid.uuid4()
    )
    assert rows[0].meta == chunked.meta_for(chunked.chunks[0])


# --- token count --------------------------------------------------------------


def test_token_count_is_a_positive_estimate_that_scales_with_length() -> None:
    short, long = estimate_token_count("hello"), estimate_token_count("hello " * 100)
    assert 0 < short < long


# --- async facade -------------------------------------------------------------


async def test_async_facade_matches_sync() -> None:
    parsed = _parsed(_sentences(30))
    via_thread = await chunk_document(parsed, embedding_model=MODEL, settings=_settings())
    direct = chunk_document_sync(parsed, embedding_model=MODEL, settings=_settings())
    assert [c.chunk_hash for c in via_thread.chunks] == [c.chunk_hash for c in direct.chunks]


# --- placement (R-31: chunk in the worker, never in the API process) -----------


def test_no_api_module_imports_the_chunker() -> None:
    api_dir = Path(__file__).resolve().parents[1] / "app" / "api"
    offenders = [
        path.name
        for path in api_dir.glob("*.py")
        if "ingestion.chunker" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} import the chunker into the API process — R-31 (§8.12) requires "
        "ingestion work to happen in the worker"
    )
