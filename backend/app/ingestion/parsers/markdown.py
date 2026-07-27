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
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.config import ParserSettings
from app.ingestion.parsers.base import (
    CharBudget,
    CorruptDocumentError,
    NoExtractableTextError,
    ParsedBlock,
    ParsedDocument,
    section_locator,
    split_text,
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
    buffer: list[str] = []
    section_index = 0
    section_line_start = 1
    last_line = 1
    pending_heading: int | None = None
    in_table = False
    row: list[str] = []
    list_depth = 0
    last_kind: str | None = None

    def add(text: str, kind: str | None = None) -> None:
        """Append a unit of text, merging runs of the same kind.

        List items and table rows are separate tokens but one logical block; joining
        them with a blank line would make the chunker treat every bullet as its own
        paragraph and scatter a table across chunk boundaries.
        """
        nonlocal last_kind
        if kind is not None and kind == last_kind and buffer:
            buffer[-1] = f"{buffer[-1]}\n{text}"
        else:
            buffer.append(text)
        last_kind = kind

    def flush(path: tuple[str, ...], line_start: int, line_end: int) -> None:
        nonlocal section_index, last_kind
        content = normalize("\n\n".join(buffer))
        buffer.clear()
        last_kind = None
        if is_blank(content):
            return
        budget.add(content)
        section_index += 1
        locator = section_locator(
            path,
            section_index,
            fallback_label=f"lines {line_start}–{line_end}",  # TBD(§8.4)
            line_start=line_start,
            line_end=line_end,
        )
        for part in split_text(content, limits.max_block_chars):
            blocks.append(ParsedBlock(text=part, locator=locator, order=len(blocks)))

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
                    row.append(rendered.strip())
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
            case "table_close":
                in_table = False
                last_kind = None  # two adjacent tables stay separate blocks
            case "tr_open":
                row = []
            case "tr_close":
                if any(cell for cell in row):
                    add(" | ".join(row), "table")
                row = []
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
