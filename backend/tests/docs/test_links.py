"""Every internal link, anchor and section reference in the documentation resolves (T-705).

Markdown does not complain about a link into nowhere, and neither does a reader who never follows
it. ``tests/test_http_docs.py``'s ``test_every_internal_anchor_resolves`` had this shape for the one
machine-written document and said in its own docstring that T-705 generalises it; this is that
generalisation, plus the two reference forms a link checker cannot see.

**Section references are the substantive half.** They are prose, not markup -- ``DEPLOYMENT.md
section 9.2``, ``(section 5)``, ``[SECURITY.md section 11.3](SECURITY.md)`` -- so nothing has ever
checked them, while `docs/DEPLOYMENT.md`'s numbering is load-bearing enough that T-702 subdivided
section 9 rather than insert a top-level section and renumber ~20 citations.

**What this deliberately does not check**, so the scope reads as a decision rather than an
oversight: external ``http(s)`` links, because a test that reaches the network fails on the train;
link *text* matching the target's title, because that is editorial; and any reference this package
attributes to the specification, because the specification is gitignored and unreadable here.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable

from tests.docs import (
    DOCUMENTS,
    PRIVATE_PREFIXES,
    REPO_ROOT,
    SPEC_TOP_SECTIONS,
    Doc,
    Markdown,
    Scope,
    SectionRef,
    ascii_only,
    by_path,
    documents,
    hand_written,
)

#: Directories a `**/*.md` walk finds and no human maintains.
_NOT_DOCUMENTATION = ("node_modules", ".venv", ".git", "__pycache__", ".pytest_cache", "dist")


def _resolve_target(written: str) -> tuple[Doc | None, list[Doc]]:
    """Attribute a written filename to a published document.

    Returns the match and the full candidate list, because a name matching more than one document
    is an ambiguity to report rather than a coin to toss: seven published files are named
    ``README.md``.
    """
    written = written.lstrip("./")
    if "/" in written:
        exact = [d for d in DOCUMENTS if d.path == written or d.path.endswith("/" + written)]
    else:
        exact = [d for d in DOCUMENTS if d.name == written]
    return (exact[0] if len(exact) == 1 else None), exact


def _cites(page: Markdown) -> Iterable[tuple[SectionRef, Doc | None, list[Doc]]]:
    for ref in page.refs:
        if ref.target is None:
            yield ref, None, []
        else:
            doc, candidates = _resolve_target(ref.target)
            yield ref, doc, candidates


def test_every_internal_link_target_exists() -> None:
    broken: list[str] = []
    for page in documents():
        for link in page.links:
            if link.target is None or (REPO_ROOT / link.target).exists():
                continue
            broken.append(f"  {page.path}:{link.line}  {ascii_only(link.href)} -> {link.target}")

    assert not broken, (
        f"{len(broken)} documentation link(s) point at files that do not exist:\n"
        + "\n".join(sorted(broken))
        + "\n\nFix the path or add the file. Markdown does not complain about a link into "
        "nowhere, which is why this is a test."
    )


def test_no_link_points_at_a_path_the_public_repository_does_not_ship() -> None:
    """The one way this guard can be green here and wrong for every reader.

    `planning/`, `CLAUDE.md`, the specification and the generated reference are all gitignored, so
    a link into them resolves on this machine and 404s in the published repository. The two
    citations of the specification that do exist (`backend/README.md`, `frontend/README.md`) are
    backticked prose rather than links, and this is what keeps them that way.
    """
    private: list[str] = []
    for page in documents():
        for link in page.links:
            if link.target is None:
                continue
            for prefix, reason in PRIVATE_PREFIXES.items():
                if link.target == prefix.rstrip("/") or link.target.startswith(prefix):
                    private.append(f"  {page.path}:{link.line}  {link.target}  ({reason})")

    assert not private, (
        f"{len(private)} link(s) point at paths the published repository does not carry:\n"
        + "\n".join(sorted(private))
        + "\n\nCite the path in backticks as prose instead, or link to something that ships."
    )


def test_every_anchor_resolves_to_a_heading_slug_or_an_explicit_id() -> None:
    pages = by_path(documents())
    broken: list[str] = []
    for page in documents():
        for link in page.links:
            if link.anchor is None:
                continue
            target = page if link.target is None else pages.get(link.target)
            if target is None:
                continue  # the link guard above owns a target that is missing or unpublished
            if link.anchor not in target.anchors:
                broken.append(
                    f"  {page.path}:{link.line}  #{ascii_only(link.anchor)}"
                    f"  (not a heading in {target.path})"
                )

    assert not broken, (
        f"{len(broken)} anchor(s) point at a heading that does not exist:\n"
        + "\n".join(sorted(broken))
        + "\n\nAn anchor is the lower-cased heading with punctuation dropped and spaces hyphenated,"
        " so renaming a heading breaks every link into it silently."
    )


def test_no_two_headings_in_one_document_share_an_anchor_slug() -> None:
    """Not cosmetic: GitHub gives the second `-1`, so one link into it lands on the other."""
    duplicated = {
        page.path: sorted(ascii_only(a) for a in page.duplicate_anchors)
        for page in documents()
        if page.duplicate_anchors
    }
    assert not duplicated, (
        "headings collide on their anchor slug, so a link into one silently reaches the other:\n"
        + "\n".join(f"  {path}: {names}" for path, names in sorted(duplicated.items()))
    )


def test_every_qualified_section_reference_names_a_real_section() -> None:
    """A reference that names its document -- the form `docs/LIMITATIONS.md` uses throughout.

    `SECURITY.md section 11.3` is item 3 of the ordered list under `## 11. Known limitations`, not
    a heading, so deleting a list item renumbers every citation after it and nothing says so.
    """
    pages = by_path(documents())
    broken: list[str] = []
    for page in documents():
        for ref, doc, candidates in _cites(page):
            if ref.target is None:
                continue
            if doc is None:
                detail = (
                    "names no published document"
                    if not candidates
                    else f"is ambiguous: {[c.path for c in candidates]}"
                )
                broken.append(
                    f"  {page.path}:{ref.line}  {ref.target} section {ref.number} {detail}"
                )
                continue
            target = pages[doc.path]
            if ref.number not in target.sections:
                highest = max(
                    target.sections, key=lambda n: [int(p) for p in n.split(".")], default="none"
                )
                broken.append(
                    f"  {page.path}:{ref.line}  {doc.path} section {ref.number}"
                    f"  (that document stops at {highest})"
                )

    assert not broken, (
        f"{len(broken)} section reference(s) name a section that does not exist:\n"
        + "\n".join(sorted(broken))
        + "\n\nA section resolves to a numbered heading (## 9. / ### 9.3) or to an item of the "
        "ordered list under a numbered heading (SECURITY.md section 11.3 is item 3 of "
        '"## 11. Known limitations").'
    )


def test_every_bare_section_reference_in_a_self_scoped_document_resolves() -> None:
    """An unqualified reference in a document that numbers its own headings means that document.

    The escape: a number above the document's own highest section, and no higher than the
    specification has sections, is read as a specification citation. `docs/ACCEPTANCE.md` tops at 5
    and cites section 8.80 -- a ruling. **This is the one deliberate hole in the guard**: a typo'd
    section 20 in `docs/ARCHITECTURE.md`, which has 17, escapes the same way. Bounded by the
    contiguity test below and by the ceiling above, and named here rather than left to be
    discovered.
    """
    broken: list[str] = []
    for page in documents():
        if page.doc.scope is not Scope.SELF:
            continue
        for ref in page.refs:
            if ref.target is not None or ref.explicit_spec:
                continue
            if ref.number in page.sections:
                continue
            if ref.top > page.highest_section and ref.top <= SPEC_TOP_SECTIONS:
                continue  # a specification citation; unreadable here, so unchecked
            broken.append(
                f"  {page.path}:{ref.line}  section {ref.number}"
                f"  (this document has sections 1..{page.highest_section})"
            )

    assert not broken, (
        f"{len(broken)} section reference(s) point at a section of their own document that does "
        "not exist:\n" + "\n".join(sorted(broken)) + "\n\nName the other document if the reference "
        'is not to this one ("DEPLOYMENT.md section 9.2"), or write "spec" before it if it cites '
        "the specification."
    )


def test_no_section_reference_cites_a_specification_section_that_cannot_exist() -> None:
    """The ceiling that stops the escape hatch swallowing everything."""
    impossible: list[str] = []
    for page in documents():
        if page.doc.scope is not Scope.SPEC:
            continue
        for ref in page.refs:
            if ref.target is None and ref.top > SPEC_TOP_SECTIONS:
                impossible.append(f"  {page.path}:{ref.line}  section {ref.number}")

    assert not impossible, (
        f"{len(impossible)} reference(s) cite a specification section that cannot exist "
        f"(it has {SPEC_TOP_SECTIONS} numbered sections):\n" + "\n".join(sorted(impossible))
    )


def test_the_numbered_heading_convention_still_holds_in_every_self_scoped_document() -> None:
    """The premise the two tests above rest on, asserted rather than assumed.

    If `## 9. Operations` were renamed to `## Operations`, every bare reference to section 9 would
    silently reclassify as an unverifiable specification citation and the guard would go green on a
    document it had stopped checking. It fails here instead, and says which document.
    """
    broken: list[str] = []
    for page in documents():
        if page.doc.scope is not Scope.SELF:
            continue
        numbers = [int(n) for n in page.numbered_headings]
        if numbers != list(range(1, len(numbers) + 1)):
            broken.append(f"  {page.path}: top-level headings are numbered {numbers}")

    assert not broken, (
        "a document declared as numbering its own sections no longer does so contiguously:\n"
        + "\n".join(sorted(broken))
        + "\n\nEvery bare section reference in these documents is resolved against this "
        "numbering. Restore it, or move the document to Scope.SPEC with a reason."
    )


def test_every_spec_scoped_document_says_why() -> None:
    silent = [d.path for d in DOCUMENTS if d.scope is Scope.SPEC and not d.reason.strip()]
    assert not silent, (
        "these documents opt out of section-reference checking with no reason given: "
        f"{sorted(silent)}\nA scope is a claim about how the document is written; it needs a "
        "sentence, or the next reader cannot tell a decision from a default."
    )


def test_the_manifest_and_the_published_tree_describe_the_same_documents() -> None:
    """A new document joins the guards by failing until somebody declares its scope."""
    on_disk: set[str] = set()
    for path in REPO_ROOT.rglob("*.md"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(part in _NOT_DOCUMENTATION for part in path.parts):
            continue
        if any(relative == p.rstrip("/") or relative.startswith(p) for p in PRIVATE_PREFIXES):
            continue
        on_disk.add(relative)

    declared = {d.path for d in DOCUMENTS}
    assert on_disk == declared, (
        "the documentation manifest and the tree disagree.\n"
        f"  published but undeclared: {sorted(on_disk - declared)}\n"
        f"  declared but absent:      {sorted(declared - on_disk)}\n\n"
        "Add a Doc(...) to tests/docs/__init__.py naming the file's section-reference scope."
    )


def test_the_reader_saw_the_hand_written_documentation() -> None:
    """Anti-vacuity, and it excludes the generated file on purpose.

    `docs/HTTP_API.md` carries most of the links and every anchor in the tree, so a sentinel that
    counted it could stay green while every hand-written document emptied out.
    """
    pages = tuple(hand_written(documents()))
    assert len(pages) >= 20, f"only {len(pages)} hand-written documents were read"
    assert sum(len(p.links) for p in pages) > 50, "the reader found almost no links"
    assert sum(len(p.refs) for p in pages) > 50, "the reader found almost no section references"
    assert sum(1 for p in pages if p.sections) >= 10, (
        "no document appears to have numbered sections"
    )

    checked = sum(
        1
        for page in pages
        for ref, doc, _ in _cites(page)
        if doc is not None or (page.doc.scope is Scope.SELF and ref.number in page.sections)
    )
    assert checked > 80, (
        f"only {checked} section references resolve to something this guard checks; the "
        "attribution ladder has probably stopped attributing"
    )


def test_the_documentation_directory_is_where_the_manifest_says_it_is() -> None:
    assert (REPO_ROOT / "docs").is_dir(), f"no docs/ under {REPO_ROOT}; REPO_ROOT is wrong"
    assert isinstance(REPO_ROOT, pathlib.Path)
