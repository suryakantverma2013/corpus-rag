"""Markdown parsing via markdown-it-py (T-203, §10.1, FR-KBM-05).

Markdown could be embedded verbatim — it is already text — but that would push syntax
noise into the embeddings: `**bold**`, `[label](https://long.url/path)` and table pipes
all become tokens the model has to spend attention on, and a citation would quote raw
markup back at the user. Tokenising strips the markup while keeping the content, and the
token stream carries two things a naive split cannot recover: the heading hierarchy, and
each token's **source line range**, which becomes the locator.

**Raw HTML is dropped, not rendered and not passed through.** A `<script>` or a hidden
`<div>` in an uploaded document is an injection vector aimed at whatever eventually reads
the chunk, and retrieved text is data, not instructions (NFR-SEC-05). Markdown's HTML
passthrough is the one place in this format where an attacker chooses the payload, so it
does not enter the corpus.

Link URLs are dropped and link *text* kept, for the same reason: a URL adds no retrievable
meaning, and a chunk that reads "click here https://…" is a phishing string the answer
would happily quote.

**A table is its own block inside its section (FR-ING-08, R-89, T-223).** It used to be one
element of the section buffer and shipped inside the prose block around it, marked `text` with
no declared header, so the chunker's row-atomicity and repeated-header rules — both already
generic — were never reached from this format. Two things make the change cheaper here than in
`docx.py`: markdown-it emits `thead`, so the header row is *declared* by the document rather
than guessed, and it consumes the `|---|` delimiter row itself, so there has never been
anything to drop. The section's `line_start`/`line_end` stay the **section's** on every block
of it, including the table's — see :func:`parse`'s `flush` for why.
"""

from __future__ import annotations

from dataclasses import replace

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    Extraction,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    Segment,
    clears_table_floor,
    emit_blocks,
    render_row,
    section_locator,
)
from app.ingestion.parsers.text import is_blank, normalize

SUFFIX = ".md"

#: CommonMark plus tables — the one GFM extension that changes *content* extraction
#: rather than styling. Without it a table is a run of pipe-laden paragraphs.
_MARKDOWN = MarkdownIt("commonmark").enable("table")

#: Inline children that contribute characters. Everything else (emphasis markers, link
#: open/close, HTML) is either a delimiter we strip or a payload we refuse.
_TEXT_CHILDREN = frozenset({"text", "code_inline", "image"})


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorruptDocumentError(f"Markdown is not valid UTF-8: {exc}") from exc


def _render_inline(token: Token) -> str:
    """Flatten an inline token to plain text."""
    parts: list[str] = []
    for child in token.children or ():
        if child.type in _TEXT_CHILDREN:
            # For an image the content is its alt text — the only human-readable part.
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        # html_inline, link_open/close and emphasis markers deliberately contribute
        # nothing: the first is refused, the rest are punctuation around text that the
        # `text` children already supply.
    return "".join(parts)


def parse(payload: bytes, *, limits: ParserSettings) -> ParsedDocument:
    """Extract heading-scoped text from a Markdown document."""
    source = _decode(payload)
    try:
        tokens = _MARKDOWN.parse(source)
    except Exception as exc:
        raise CorruptDocumentError(f"Markdown could not be parsed: {exc}") from exc

    budget = CharBudget(limits.max_extracted_chars)
    blocks: list[ParsedBlock] = []
    heading_path: tuple[str, ...] = ()
    segments: list[Segment] = []
    section_index = 0
    section_line_start = 1
    last_line = 1
    pending_heading: int | None = None
    in_table = False
    in_head = False
    table_lines: list[str] = []
    header_row = ""
    columns = 0
    row: list[str] = []
    list_depth = 0
    last_kind: str | None = None

    def add(text: str, kind: str | None = None) -> None:
        """Append a unit of prose, merging runs of the same kind.

        Two joins, and both were already here — one inside a buffer element and one in
        `flush`'s `"\\n\\n".join`. List items are separate tokens but one logical block, so a
        run of one kind joins with a single newline; anything else joins with a blank line.
        What T-223 adds is only the boundary: a table segment is never merged into, so prose
        after a table starts a fresh block instead of being run together with the prose before
        it, which they never were adjacent to.
        """
        nonlocal last_kind
        separator = "\n" if kind is not None and kind == last_kind else "\n\n"
        last_kind = kind
        if segments and segments[-1].extraction is Extraction.TEXT:
            segments[-1] = replace(segments[-1], text=f"{segments[-1].text}{separator}{text}")
        else:
            segments.append(Segment(text=text))

    def flush(path: tuple[str, ...], line_start: int, line_end: int) -> None:
        """Emit this section's segments as an ordered run of blocks on **one** locator.

        The line range is the **section's**, and every block of the section carries it — a
        table on lines 16–19 of a section spanning 12–20 says "lines 12–20". Narrowing it is
        possible here (`table_open.map` has the range) and impossible in `docx.py`, and one
        format citing a sub-section range while the other cannot would make the same content
        read differently in the FR-CIT-03 card depending on which file it arrived in. R-88(6)
        settled the same question for a PDF table, which shares its page's locator rather than
        taking a region of its own. **Revisit trigger:** a measured complaint that a
        section-wide range is too coarse to find a table in a long document.

        See `docx.py`'s twin for why `section_index` moves once per section, not per block.
        """
        nonlocal section_index, last_kind
        pending = [replace(segment, text=normalize(segment.text)) for segment in segments]
        segments.clear()
        last_kind = None
        pending = [segment for segment in pending if not is_blank(segment.text)]
        if not pending:
            return
        section_index += 1
        locator = section_locator(
            path,
            section_index,
            fallback_label=f"lines {line_start}–{line_end}",  # TBD(§8.4)
            line_start=line_start,
            line_end=line_end,
        )
        for segment in pending:
            emit_blocks(
                blocks,
                segment.text,
                locator=locator,
                budget=budget,
                limits=limits,
                extraction=segment.extraction,
                headed=segment.headed,
            )

    for token in tokens:
        match token.type:
            case "heading_open":
                # Flushed *before* `last_line` absorbs this token, so the outgoing
                # section ends on its own last line rather than on the heading that
                # starts the next one.
                flush(heading_path, section_line_start, last_line)
                pending_heading = int(token.tag[1:])
                section_line_start = token.map[0] + 1 if token.map else last_line
            case "inline":
                rendered = _render_inline(token)
                if pending_heading is not None:
                    heading = rendered.strip()
                    # An H1 → H3 jump yields a two-element path, not a padded one.
                    heading_path = heading_path[: pending_heading - 1] + (heading,)
                    pending_heading = None
                    if heading:
                        add(heading)
                elif in_table:
                    # Unstripped: `render_row` owns cell whitespace for every format, and it
                    # also collapses a softbreak inside a cell to a space, which the bare join
                    # this replaces let through as a literal newline — one row on two lines,
                    # the exact adjacency the pipe rendering exists to preserve.
                    row.append(rendered)
                elif not is_blank(rendered):
                    if list_depth:
                        add(f"- {rendered}", "list")
                    else:
                        add(rendered)
            case "fence" | "code_block":
                # Code is content: config snippets and commands are exactly what a
                # technical corpus gets asked about. Kept verbatim, markers removed.
                if token.content.strip():
                    add(token.content.strip("\n"))
            case "table_open":
                in_table = True
                in_head = False
                table_lines = []
                header_row = ""
                columns = 0
            case "thead_open":
                # markdown-it consumes the `|---|` delimiter row inside its `table` rule and
                # emits no token for it, so there is nothing to drop — measured, and the half
                # of this that turned out to need no code at all. What it *does* emit is
                # `thead`, which makes the header free and structural: the row inside it is
                # the header row because the document said so, not because we guessed.
                in_head = True
            case "thead_close":
                in_head = False
            case "tr_open":
                row = []
            case "tr_close":
                line = render_row(row)
                if line.strip(" |"):
                    if not table_lines:
                        columns = len(row)
                    if in_head:
                        # GFM permits exactly one `thead` row and markdown-it emits exactly
                        # one `tr` inside it — measured, not assumed — so this runs at most
                        # once per table and `header_row` is line 0 of `table_lines`, which is
                        # the invariant `emit_blocks` re-reads. A guard against a second row
                        # was written here and deleted: it could not be reached, and an
                        # unreachable branch is a claim no test can keep honest.
                        header_row = line
                    table_lines.append(line)
                row = []
            case "table_close":
                in_table = False
                last_kind = None  # a list interrupted by a table does not re-merge
                if table_lines:
                    tabular = clears_table_floor(
                        rows=len(table_lines), columns=columns, limits=limits
                    )
                    text = "\n".join(table_lines)
                    if tabular:
                        segments.append(
                            Segment(
                                text=text,
                                extraction=Extraction.TABLE,
                                # A header with no body row under it labels nothing.
                                headed=bool(header_row) and len(table_lines) > 1,
                            )
                        )
                    else:
                        add(text, "table")
                table_lines = []
            case "bullet_list_open" | "ordered_list_open":
                list_depth += 1
            case "bullet_list_close" | "ordered_list_close":
                list_depth = max(0, list_depth - 1)
                if not list_depth:
                    last_kind = None
            case "hr":
                add("---")
            case "html_block":
                pass  # refused — see the module docstring

        if token.map:
            last_line = max(last_line, token.map[1])

    flush(heading_path, section_line_start, last_line)

    if not blocks:
        raise NoExtractableTextError("this Markdown file contains no readable text")

    # `page_count` stays None — Markdown has no pagination (R-34).
    return ParsedDocument(suffix=SUFFIX, blocks=tuple(blocks))
