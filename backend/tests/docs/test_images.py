"""`docs/images/` — the only files here a test cannot read the *contents* of (T-706).

These are the repository's first committed binaries, and they were admitted on the condition
that regenerating them is one command (`npm run docs:shots`) against a corpus we seed
(`tools.seed_demo`), rather than someone with a cropping tool.

**What this guard can and cannot do, stated plainly.** It checks both directions of the
reference — every image a document embeds exists, and every image on disk is embedded by
something. That catches a rename, a deletion, and a shot captured under a name nothing uses.

It **cannot** tell you a screenshot is out of date. Nothing cheap can: the picture is of a
surface, and comparing it to that surface is the fidelity harness's job, not a unit test's
(R-66(5) ruled screenshot diffing out for this project, on font-load timing and antialiasing).
So staleness is a real, accepted cost of illustrating the manual, and the mitigation is that
recapturing is cheap — not that anything detects the need.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"
IMAGES = DOCS / "images"

#: Markdown image embeds: `![alt](images/name.png)`.
_EMBED = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _referenced() -> dict[str, list[str]]:
    """`{image filename: [documents that embed it]}`."""
    found: dict[str, list[str]] = {}
    for document in sorted(DOCS.glob("*.md")):
        for target in _EMBED.findall(document.read_text(encoding="utf-8")):
            name = target.rsplit("/", 1)[-1]
            found.setdefault(name, []).append(document.name)
    return found


def _on_disk() -> set[str]:
    return {path.name for path in IMAGES.glob("*")} if IMAGES.is_dir() else set()


def test_every_embedded_image_exists() -> None:
    """A broken image in the only documentation the public repository ships."""
    missing = sorted(
        f"{name} (embedded by {', '.join(docs)})"
        for name, docs in _referenced().items()
        if name not in _on_disk()
    )
    assert not missing, "embedded but absent from docs/images/:\n  " + "\n  ".join(missing)


def test_every_committed_image_is_used() -> None:
    """The other direction, and the one that rots quietly.

    A screenshot whose section was rewritten away is dead weight nobody notices: it stays in the
    repository, it is still regenerated on every capture run, and a reader diffing the commit
    cannot tell it from a live one.
    """
    orphans = sorted(_on_disk() - set(_referenced()))
    assert not orphans, "in docs/images/ but embedded nowhere: " + ", ".join(orphans)


def test_the_images_are_pngs_and_not_enormous() -> None:
    """The size limit is a policy, not an optimisation.

    This repository had **no** binary files before T-706, so what is committed here should stay
    reviewable. A megabyte-scale screenshot usually means a capture at the wrong device scale
    (`cdp.mjs` pins it to 1) or a full-page shot where a viewport was intended.
    """
    oversize = sorted(
        f"{path.name} ({path.stat().st_size // 1024} KB)"
        for path in IMAGES.glob("*")
        if path.stat().st_size > 400 * 1024
    )
    assert not oversize, "unexpectedly large screenshots: " + ", ".join(oversize)

    wrong_type = sorted(path.name for path in IMAGES.glob("*") if path.suffix != ".png")
    assert not wrong_type, "docs/images/ holds only PNGs: " + ", ".join(wrong_type)


def test_the_user_guide_is_illustrated() -> None:
    """Anti-vacuity: with no images at all, both directions above pass trivially.

    T-706's whole premise is a manual with pictures in it, so the guard has to assert that the
    pictures exist rather than only that the two lists agree.
    """
    guide = DOCS / "USER_GUIDE.md"
    assert guide.is_file(), "docs/USER_GUIDE.md is missing"
    embeds = _EMBED.findall(guide.read_text(encoding="utf-8"))
    assert len(embeds) >= 8, (
        f"the guide embeds only {len(embeds)} image(s); it is meant to be illustrated"
    )
