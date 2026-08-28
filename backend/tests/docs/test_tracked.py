"""A document may only reference files the repository actually has (T-729).

**Raised by a real failure, the moment T-706 shipped.** `docs/USER_GUIDE.md` was committed while
the nine images it embeds and `frontend/docs-shots/capture.mjs` were left untracked. Everything
in this package passed, because every other guard here resolves a reference against the
**working tree** — where the files were sitting, unstaged. The defect existed only in git, and
would have surfaced for whoever cloned next: a manual with every picture broken, and an
`npm run docs:shots` script pointing at nothing.

So this guard asks a different question from its neighbours. Not *does this path exist* but
*does the repository contain it*. That distinction is invisible on the machine where the work
was done, which is exactly why it needs a test rather than care.

**Scope is the demonstrated gap**: markdown link and image targets that name a local file.
Prose that merely mentions a path is `test_links.py`'s business, and a reference to a directory
is deliberately not handled — none exists today, and inventing a rule for a case with no
instance is how a guard acquires behaviour nobody can justify later. If one appears, this fails
and whoever adds it decides what it should mean.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.docs import REPO_ROOT, documents


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def _is_git_checkout() -> bool:
    """Whether git can answer questions about this tree at all.

    A guard must never be the reason a suite cannot run somewhere — an exported tarball or a
    vendored copy is a legitimate way to receive this code, and neither has a `.git`.
    """
    try:
        return _git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except OSError:  # git is not installed
        return False


requires_git = pytest.mark.skipif(
    not _is_git_checkout(), reason="not a git checkout; nothing to compare the tree against"
)


def _tracked() -> frozenset[str]:
    """Every path git has, POSIX-relative to the repository root."""
    return frozenset(_git("ls-files").stdout.split("\n")) - {""}


def _references() -> dict[str, set[str]]:
    """`{repo-relative target: documents that reference it}`, links and images together."""
    found: dict[str, set[str]] = {}
    for parsed in documents():
        for ref in (*parsed.links, *parsed.images):
            if ref.target:
                found.setdefault(ref.target, set()).add(parsed.path)
    return found


@requires_git
def test_every_referenced_file_is_tracked_by_git() -> None:
    """The check the working tree cannot make.

    A path that exists locally and is absent from git is the shape T-706 shipped in: correct on
    the machine that produced it, broken for everyone else, and silent in between.
    """
    tracked = _tracked()
    missing = sorted(
        f"{target} (referenced by {', '.join(sorted(docs))})"
        for target, docs in _references().items()
        if target not in tracked
    )
    assert not missing, (
        "referenced by the documentation but absent from git — `git add` them, or the next "
        "clone gets broken references:\n  " + "\n  ".join(missing)
    )


@requires_git
def test_the_guard_examined_something() -> None:
    """Anti-vacuity, and not a formality.

    With no references collected the assertion above passes on an empty set, which is precisely
    how a guard ends up certifying rather than checking (§8.65(5), and T-606's 39 checks that
    silently measured the wrong page). Two floors: the reference reader found a substantial
    number of targets, and **images are among them** — images being the kind that actually
    broke, and the kind `Markdown.images` was added to see.
    """
    references = _references()
    assert len(references) >= 20, (
        f"only {len(references)} local target(s) found; the reference reader has probably "
        "stopped seeing links"
    )
    images = [target for target in references if target.startswith("docs/images/")]
    assert images, "no image references found, yet images are what this guard exists for"


@requires_git
def test_git_answers_at_all() -> None:
    """`git ls-files` returning nothing would make the main check vacuous *and* pass.

    Belt and braces on the instrument rather than the subject: an empty result is
    indistinguishable from "everything is tracked" in the assertion above.
    """
    assert len(_tracked()) > 100, (
        "git ls-files returned almost nothing; the guard is not reading the repository"
    )
