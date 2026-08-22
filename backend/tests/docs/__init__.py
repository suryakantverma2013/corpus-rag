"""The documentation manifest and the markdown reader the guards share (T-705).

`docs/` is the only documentation the public repository ships, and until now **nothing stopped any
of it rotting.** This project already guards spec-to-code drift in three places
(``test_env_templates``, ``test_spec_xref``, ``tests/acceptance/``) precisely because prose goes
stale silently, and Phase 7 produced ~7,000 lines of it. The evidence is local and recent: T-701
repaired two stale task markers in ``backend/README.md``; T-703 found its own module map claiming
ten guarded modules where seven are guarded; T-704 found the test counts stale in *every row* of
two tables, and an anchor pointing at ``#4-...`` for a section numbered 6. Each was caught by a
throwaway checker that was then thrown away. These are those checkers, kept.

**Everything in the tree resolved on the day this was written.** A green run is therefore not what
this package delivers -- every guard here was shown to fail on a specific mutation before it was
committed, because a guard that cannot fail does not verify, it certifies.

**Why a real markdown parser.** ``markdown-it-py`` is already a declared backend dependency
(``app/ingestion/parsers/markdown.py``), so using it is not the package-policy defect that
importing PyYAML would be in ``test_env_templates.py``. Three of the traps a hand-rolled regex
falls into disappear as a consequence rather than as a special case:

* fenced code is a token, so ``docs/TESTING.md``'s ``# set PARSER_OCR_ENABLED=true`` inside a bash
  block never mints a phantom heading, a phantom variable or a phantom section reference;
* **inline** code is a token, so ``docs/CONFIGURATION.md``'s ``# TBD(§8.4)`` -- a marker quoted as
  an example -- drops out of the reference scan without anyone writing a rule about it;
* every link form (``[x](y)``, ``[x](y "title")``, ``[x](<y>)``, reference-style, autolink) arrives
  as one ``link_open`` href. The tree happens to contain none of the exotic forms *today*, which is
  exactly why a regex would look correct and rot the day somebody writes one.

**Output is ASCII.** A section-sign glyph reaching a cp1252 console raises ``UnicodeEncodeError``
inside the failure message, which reads like a defect in the guard rather than in the document
(``tools/acceptance.py`` has the same rule for the same reason). Failures say ``section 11.13``.

**Nothing here reads the specification, and nothing may.** Every input is committed -- the markdown
itself, ``backend/openapi.json``, ``frontend/package.json``, the compose files, ``app/config.py``.
So unlike ``test_spec_xref.py`` and ``tests/acceptance/`` there is no ``requires_spec`` gate in this
package, and there must never be one: these guards run identically in a public clone, and *a test
that has only ever skipped is not a passing test* (Rev 0.12).
"""

from __future__ import annotations

import functools
import pathlib
import re
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from markdown_it import MarkdownIt
from markdown_it.token import Token

__all__ = [
    "BACKEND_ROOT",
    "DOCUMENTS",
    "PRIVATE_PREFIXES",
    "REPO_ROOT",
    "SPEC_TOP_SECTIONS",
    "Code",
    "Doc",
    "Link",
    "Markdown",
    "Scope",
    "SectionRef",
    "ascii_only",
    "by_path",
    "documents",
    "github_slug",
    "hand_written",
    "read",
]

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[3]
BACKEND_ROOT: Final = REPO_ROOT / "backend"

#: The specification's top-level section count. It bounds the one escape hatch in the
#: section-reference ladder: a bare reference this package cannot attribute to a document is
#: allowed to be a specification citation, but only to a section the specification could have.
#: The spec is gitignored, so this is a committed number rather than a parsed one -- the same
#: trade `tools/spec_issues.json` makes, and for the same reason.
SPEC_TOP_SECTIONS: Final = 11

#: Paths the published repository does not carry, name -> reason. Read off `.gitignore`'s
#: "Private to this working copy (not published)" block. A documentation link into any of these
#: resolves on the author's machine and 404s for every reader, which is the one way a link guard
#: can be green here and wrong in a public clone.
PRIVATE_PREFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "planning/": "the internal build board and phase plans, gitignored",
        "docs/reference/": "generated API reference; regenerated locally, never committed",
        "ReferenceDocs/": "course-supplied sources, not ours to relicense",
        "RAG Chatbot GUI Design/": "the third-party design handoff, not ours to relicense",
        "CLAUDE.md": "agent orientation, gitignored",
        ".claude/": "project skills and settings, gitignored",
        "Nexus_AI_Detailed_Specification.md": "the specification, gitignored",
        "backups/": "backup output, never committed",
    }
)


class Scope(StrEnum):
    """What a *bare* section reference means in a given document.

    ``SELF`` documents number their own top-level headings, so ``section 9`` written in one of
    them means that document's section 9. ``SPEC`` documents have no numbered headings at all, so
    the only thing a bare number there can mean is the specification -- which this package cannot
    open, and therefore does not check.
    """

    SELF = "self"
    SPEC = "spec"


@dataclass(frozen=True, slots=True)
class Doc:
    """One published markdown file.

    ``reason`` is required for a ``SPEC``-scoped document and is printed by the guard that checks
    it. A scope field rather than an allowance list is what lets `docs/MODULE_MAP.md`'s bare
    "section 11 production scenarios" (the specification's) and `docs/DEPLOYMENT.md`'s bare
    "section 11 runs the same journey" (its own) both be right.
    """

    path: str
    scope: Scope
    reason: str = ""
    generated: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def file(self) -> pathlib.Path:
        return REPO_ROOT / self.path


_SPEC_BY_CONVENTION = (
    "a component README: it has no numbered headings, and cites the specification by convention"
)

#: Every published markdown file. `test_links.py` reconciles this against the tree both ways, so a
#: new document fails the suite until somebody declares what a bare section number means in it.
DOCUMENTS: Final[tuple[Doc, ...]] = (
    Doc("docs/ACCEPTANCE.md", Scope.SELF),
    Doc("docs/ARCHITECTURE.md", Scope.SELF),
    Doc("docs/CONFIGURATION.md", Scope.SELF),
    Doc("docs/DATA_MODEL.md", Scope.SELF),
    Doc("docs/DEPLOYMENT.md", Scope.SELF),
    Doc("docs/DEVELOPMENT.md", Scope.SELF),
    Doc("docs/EVALUATION.md", Scope.SELF),
    Doc("docs/LIMITATIONS.md", Scope.SELF),
    Doc("docs/SECURITY.md", Scope.SELF),
    Doc("docs/TESTING.md", Scope.SELF),
    Doc(
        "docs/HTTP_API.md",
        Scope.SPEC,
        "generated from backend/openapi.json by tools.httpdocs and guarded byte-for-byte by "
        "tests/test_http_docs.py; its headings are routes and schemas, never numbers, so the "
        "section references in its prose are the specification's",
        generated=True,
    ),
    Doc(
        "docs/MODULE_MAP.md",
        Scope.SPEC,
        "organised by package rather than by number; its bare section references cite the "
        "specification (section 11's production scenarios, section 4's surfaces)",
    ),
    Doc("README.md", Scope.SPEC, "the front page: unnumbered headings, no self-references"),
    Doc("backend/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
    Doc("deployment/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
    Doc(
        "deployment/keycloak/README.md",
        Scope.SPEC,
        "its numbered headings are sub-steps of one procedure rather than document sections, so a "
        "bare section number here is the specification's (the realm notes cite section 4.9)",
    ),
    Doc("frontend/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
    Doc(
        "frontend/ACCESSIBILITY.md",
        Scope.SPEC,
        "the conformance statement cites the specification's requirements and rulings throughout",
    ),
    Doc("frontend/a11y/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
    Doc("frontend/e2e/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
    Doc("frontend/fidelity/README.md", Scope.SPEC, _SPEC_BY_CONVENTION),
)


@dataclass(frozen=True, slots=True)
class Link:
    """One internal markdown link. External schemes are dropped by the reader."""

    href: str
    #: Repo-relative POSIX path of the target, or `None` for a link into the same document.
    target: str | None
    anchor: str | None
    line: int


@dataclass(frozen=True, slots=True)
class SectionRef:
    """One `section N` / `section N.M` citation, with whatever names its target.

    ``target`` is the filename **as the document wrote it** -- resolving that to a `Doc` is the
    guard's job, because a filename matching more than one published document is an ambiguity to
    report rather than a coin to toss (seven files here are named `README.md`).
    """

    number: str
    target: str | None
    explicit_spec: bool
    line: int

    @property
    def top(self) -> int:
        return int(self.number.split(".")[0])


@dataclass(frozen=True, slots=True)
class Code:
    """A fenced block or an inline span. ``language`` is ``"inline"`` for a span."""

    text: str
    language: str
    line: int


@dataclass(frozen=True, slots=True)
class Markdown:
    doc: Doc
    text: str
    anchors: frozenset[str]
    duplicate_anchors: frozenset[str]
    #: Section number -> how it is carried, `"heading"` or `"list item"`.
    sections: Mapping[str, str]
    numbered_headings: tuple[str, ...]
    links: tuple[Link, ...]
    refs: tuple[SectionRef, ...]
    code: tuple[Code, ...]

    @property
    def path(self) -> str:
        return self.doc.path

    @property
    def highest_section(self) -> int:
        tops = [int(n.split(".")[0]) for n in self.numbered_headings]
        return max(tops, default=0)


_MARKDOWN: Final = MarkdownIt("commonmark").enable("table")

_EXTERNAL: Final = ("http://", "https://", "mailto:", "tel:")
_SECTION: Final = re.compile(r"§\s?(\d+(?:\.\d+)*)")
_NUMBERED_HEADING: Final = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+\S")
_TRAILING_MD: Final = re.compile(r"([\w./-]+\.md)[`)\]]?[\s,;]*$")
_SPEC_WORD: Final = re.compile(
    r"\b(spec|specs|specification|specification's|spec's)\b[\s(]*$", re.I
)
_SEPARATORS: Final = re.compile(r"^[\s,;)\]/–—-]*(?:and|or)?[\s,;)\]/–—-]*$", re.I)
_HTML_ID: Final = re.compile(r'<a\s+id="([^"]+)"')
_SLUG_STRIP: Final = re.compile(r"[^\w\s-]", re.UNICODE)


def ascii_only(text: str) -> str:
    """Every doc-derived fragment interpolated into a failure message goes through this."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def github_slug(text: str) -> str:
    """GitHub's heading-anchor rule: lower-case, drop punctuation, spaces become hyphens.

    Runs of hyphens are deliberately **not** collapsed -- GitHub does not collapse them either,
    so `# Corpus -- Accessibility` anchors as `corpus--accessibility`.
    """
    slug = _SLUG_STRIP.sub("", text.strip().lower())
    return slug.replace(" ", "-")


def _plain(inline: Token) -> str:
    """The visible text of an inline token: markup dropped, code-span contents kept."""
    parts: list[str] = []
    for child in inline.children or ():
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
    return "".join(parts)


class _Lines:
    """Exact line numbers for things found in the token stream.

    A token carries the line span of its *block*, which for a long paragraph is not where the
    reference sits. Searching the source lines of that span for the thing just parsed recovers the
    real line, and remembering how many times each needle has been handed out within a span keeps
    repeats in order. Line numbers are the whole value of a failure message here, so this is worth
    the twenty lines.
    """

    def __init__(self, text: str) -> None:
        self._lines = text.split("\n")
        self._used: dict[tuple[int, int, str], int] = {}

    def find(self, span: tuple[int, int] | None, needle: str) -> int:
        if span is None:
            return 1
        start, end = span
        key = (start, end, needle)
        skip = self._used.get(key, 0)
        seen = 0
        for offset in range(start, min(end, len(self._lines))):
            if needle in self._lines[offset]:
                if seen == skip:
                    self._used[key] = skip + 1
                    return offset + 1
                seen += 1
        return start + 1


def _href_target(doc: Doc, href: str) -> str | None:
    """Resolve a link's path part to a repo-relative POSIX path, or `None` for a same-page link."""
    path = urllib.parse.unquote(href)
    if not path:
        return None
    here = pathlib.PurePosixPath(doc.path).parent
    resolved: list[str] = []
    for part in (here / path).parts:
        if part == "..":
            if resolved:
                resolved.pop()
        elif part not in {".", ""}:
            resolved.append(part)
    return "/".join(resolved)


def _sections(tokens: list[Token], lines: _Lines) -> tuple[dict[str, str], list[str]]:
    """Numbered headings, plus the ordered-list items directly under a numbered `##` heading.

    The second half is the one that matters. `docs/SECURITY.md` has no `### 11.x` headings at
    all -- its section 11 is an ordered list -- while `docs/LIMITATIONS.md` cites thirteen items of
    it by number. Resolving those against headings alone would report thirteen failures against
    correct prose, and deleting one list item silently renumbers every citation after it, which is
    precisely the drift worth catching.

    A list is only harvested when it follows its `##` heading directly: any heading in between
    (`### 9.1`) clears the pending section, so `docs/DEPLOYMENT.md`'s subsections come from their
    own headings rather than from a list somewhere under them.
    """
    sections: dict[str, str] = {}
    headings: list[str] = []
    pending: str | None = None

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            inline = tokens[index + 1]
            match = _NUMBERED_HEADING.match(_plain(inline))
            pending = None
            if match:
                number = match.group(1)
                sections.setdefault(number, "heading")
                if token.tag == "h2":
                    headings.append(number)
                    pending = number
            index += 2
            continue
        if token.type == "ordered_list_open" and pending is not None:
            depth = token.level
            item = 0
            cursor = index + 1
            while cursor < len(tokens):
                inner = tokens[cursor]
                if inner.type == "ordered_list_close" and inner.level == depth:
                    break
                if inner.type == "list_item_open" and inner.level == depth + 1:
                    item += 1
                    sections.setdefault(f"{pending}.{item}", "list item")
                cursor += 1
            pending = None
            index = cursor
            continue
        index += 1

    _ = lines
    return sections, headings


def _walk_inline(  # noqa: C901 - one pass over one token stream; splitting it hides the order
    doc: Doc,
    inline: Token,
    span: tuple[int, int] | None,
    lines: _Lines,
    links: list[Link],
    refs: list[SectionRef],
    code: list[Code],
) -> None:
    """Collect links, section references and code spans from one inline run.

    The section-reference ladder lives here because attribution is positional: a reference is
    attributed to the link it sits inside, to a filename just written, or to the filename a
    previous reference in the same run was attributed to -- which is what carries a run like
    ``[SECURITY.md section 5, section 11.3](SECURITY.md)``.
    """
    link_href: str | None = None
    #: The last `.md` filename this run mentioned, by link, code span or plain text.
    pending: str | None = None
    #: What the previous reference in this run resolved to, for `section 5, section 11.3` runs.
    inherited: str | None = None
    #: Text since the last child, used to decide whether only separators intervened.
    since = ""

    for child in inline.children or ():
        if child.type == "link_open":
            link_href = child.attrGet("href") or ""
            if link_href.split("#")[0].endswith(".md"):
                pending = link_href.split("#")[0]
            if not link_href.startswith(_EXTERNAL):
                path, _, anchor = link_href.partition("#")
                links.append(
                    Link(
                        href=link_href,
                        target=_href_target(doc, path) if path else None,
                        anchor=anchor or None,
                        line=lines.find(span, link_href),
                    )
                )
            since = ""
            continue

        if child.type == "link_close":
            link_href = None
            since = ""
            continue

        if child.type == "code_inline":
            code.append(Code(child.content, "inline", lines.find(span, child.content)))
            if child.content.endswith(".md"):
                pending = child.content
            since = ""
            continue

        if child.type not in {"text", "softbreak", "hardbreak"}:
            continue

        text = " " if child.type != "text" else child.content
        cursor = 0
        for match in _SECTION.finditer(text):
            before = text[: match.start()]
            target: str | None = None
            if link_href and link_href.split("#")[0].endswith(".md"):
                target = link_href.split("#")[0]
            elif (trailing := _TRAILING_MD.search(before)) is not None:
                target = trailing.group(1)
            elif pending is not None and _SEPARATORS.match(since + text[cursor : match.start()]):
                target = pending
            elif inherited is not None and _SEPARATORS.match(text[cursor : match.start()]):
                target = inherited

            refs.append(
                SectionRef(
                    number=match.group(1),
                    target=target,
                    explicit_spec=bool(_SPEC_WORD.search(before)),
                    line=lines.find(span, match.group(0)),
                )
            )
            inherited = target
            cursor = match.end()
        since = text[cursor:] if any(_SECTION.finditer(text)) else since + text


def read(doc: Doc) -> Markdown:
    """Parse one document. CRLF is normalised first, so a Windows checkout parses identically."""
    text = "\n".join(doc.file.read_text(encoding="utf-8").splitlines())
    tokens = _MARKDOWN.parse(text)
    lines = _Lines(text)

    slugs: list[str] = []
    ids: list[str] = []
    links: list[Link] = []
    refs: list[SectionRef] = []
    code: list[Code] = []

    for index, token in enumerate(tokens):
        span = tuple(token.map) if token.map else None  # type: ignore[assignment]
        if token.type in {"fence", "code_block"}:
            language = token.info.strip().split(" ")[0]
            code.append(Code(token.content, language, (span or (0, 0))[0] + 1))
        elif token.type == "html_block":
            ids.extend(_HTML_ID.findall(token.content))
        elif token.type == "heading_open":
            slugs.append(github_slug(_plain(tokens[index + 1])))
        elif token.type == "inline":
            for child in token.children or ():
                if child.type == "html_inline":
                    ids.extend(_HTML_ID.findall(child.content))
            _walk_inline(doc, token, span, lines, links, refs, code)

    sections, headings = _sections(tokens, lines)
    # Counted within each kind, never across them: `docs/HTTP_API.md` writes an explicit
    # `<a id="errorresponse">` immediately above a `### \`ErrorResponse\`` heading whose slug is the
    # same string, and those two name one place. A heading slug repeated *within* a document is the
    # real defect -- GitHub disambiguates the second with `-1`, so one of the links into it lands on
    # the wrong section.
    duplicates = {a for a in slugs if slugs.count(a) > 1} | {a for a in ids if ids.count(a) > 1}

    return Markdown(
        doc=doc,
        text=text,
        anchors=frozenset(slugs) | frozenset(ids),
        duplicate_anchors=frozenset(duplicates),
        sections=MappingProxyType(sections),
        numbered_headings=tuple(headings),
        links=tuple(links),
        refs=tuple(refs),
        code=tuple(code),
    )


@functools.cache
def documents() -> tuple[Markdown, ...]:
    """Every published document, parsed once and shared by all five guards."""
    return tuple(read(doc) for doc in DOCUMENTS)


def hand_written(parsed: tuple[Markdown, ...]) -> Iterator[Markdown]:
    """The documents a person maintains.

    `docs/HTTP_API.md` supplies most of the links and anchors in the tree, so an anti-vacuity
    sentinel counting it could stay green while every hand-written document emptied out.
    """
    return (page for page in parsed if not page.doc.generated)


def by_path(parsed: tuple[Markdown, ...]) -> Mapping[str, Markdown]:
    return MappingProxyType({page.path: page for page in parsed})
