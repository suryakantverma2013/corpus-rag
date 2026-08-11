"""The spec/code cross-reference guard (T-607).

Two layers, and the split is the point. The **hard rule** — no provisional marker may cite a
settled issue — runs against the committed manifest, so it works in the public checkout where
the spec is gitignored; *a test that has only ever skipped is not a passing test* (Rev 0.12).
The **drift check** — manifest still matches the register — needs the spec and skips without
it, which is the same shape as R-57(2)'s committed `openapi.json` guard.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from tools.spec_xref import (
    MANIFEST_PATH,
    SETTLED,
    SPEC_PATH,
    STATUSES,
    Citation,
    Manifest,
    build_manifest,
    find_citations,
    load_manifest,
    parse_register,
    parse_revision,
    render_report,
    stale_markers,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

requires_spec = pytest.mark.skipif(
    not SPEC_PATH.is_file(),
    reason="Nexus_AI_Detailed_Specification.md is gitignored; the manifest carries the register",
)


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


@pytest.fixture(scope="module")
def citations() -> list[Citation]:
    return find_citations(REPO_ROOT)


# --------------------------------------------------------------------------------------
# The hard rule. Manifest-based, so it runs everywhere.
# --------------------------------------------------------------------------------------


def test_no_provisional_marker_cites_a_settled_issue(
    manifest: Manifest, citations: list[Citation]
) -> None:
    """The rule this tool exists for.

    A marker means "unresolved, needs an answer". Pointing it at an issue that has one is how
    11 sites came to advertise a question R-62(4) had closed, and how the greppable inventory
    of genuinely open questions stops being trustworthy.
    """
    stale = stale_markers(citations, manifest)
    assert stale == [], "\n".join(
        f"{c.path}:{c.line} cites {c.issue} ({manifest.issues.get(c.issue, 'unknown')})"
        for c in stale
    )


def test_every_issue_cited_in_the_tree_exists_in_the_manifest(
    manifest: Manifest, citations: list[Citation]
) -> None:
    """Catches a typo'd or renumbered id, which would otherwise read as a real reference."""
    unknown = sorted({c.issue for c in citations} - set(manifest.issues))
    assert unknown == [], f"cited but absent from the register: {unknown}"


def test_manifest_is_well_formed(manifest: Manifest) -> None:
    assert manifest.issues, "manifest carries no issues"
    assert set(manifest.issues.values()) <= set(STATUSES)
    assert manifest.revision
    # The register is contiguous from OI-15; OI-11..14 were never assigned (§8.3 note).
    numbers = sorted(int(k[3:]) for k in manifest.issues)
    assert numbers == list(range(numbers[0], numbers[-1] + 1)), "a register row is missing"


def test_manifest_file_is_sorted_and_regenerable() -> None:
    """A hand-edited manifest is the failure mode this whole guard would not survive."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    keys = list(raw["issues"])
    assert keys == sorted(keys, key=lambda k: int(k[3:])), "run `--write`, do not hand-edit"


# --------------------------------------------------------------------------------------
# Drift: the manifest against its source. Needs the spec.
# --------------------------------------------------------------------------------------


@requires_spec
def test_manifest_matches_the_spec_register(manifest: Manifest) -> None:
    from_spec = build_manifest(SPEC_PATH.read_text(encoding="utf-8"))
    assert from_spec.issues == manifest.issues, (
        "spec_issues.json is stale — run `uv run python -m tools.spec_xref --write`"
    )
    assert from_spec.revision == manifest.revision


@requires_spec
def test_every_register_row_carries_a_status_tag() -> None:
    """The tag convention is what makes the status machine-readable.

    Inferring it from prose was tried and mis-classified three rows, so an untagged row must
    fail rather than default to anything.
    """
    text = SPEC_PATH.read_text(encoding="utf-8")
    tagged = set(parse_register(text))
    import re

    mentioned = {m.group(1) for m in re.finditer(r"^\| \*\*(OI-\d+)\*\*", text, flags=re.MULTILINE)}
    untagged = mentioned - tagged
    assert untagged == set(), f"register rows missing a status tag: {sorted(untagged)}"


# --------------------------------------------------------------------------------------
# Unit tests on the parsing and reporting, against synthetic input.
# --------------------------------------------------------------------------------------

_SPEC_FIXTURE = """
| **Status** | Rev 9.9 — something happened |

| ID | Issue | Recommendation |
|---|---|---|
| **OI-15** `[RESOLVED]` | settled long ago | ... |
| **OI-16** `[DISPOSITIONED]` | deferred with a trigger | ... |
| **OI-17** `[OPEN]` | still a question | ... |
"""


def test_parse_register_reads_the_tag_not_the_prose() -> None:
    assert parse_register(_SPEC_FIXTURE) == {
        "OI-15": "RESOLVED",
        "OI-16": "DISPOSITIONED",
        "OI-17": "OPEN",
    }


def test_parse_register_rejects_a_duplicate_row() -> None:
    with pytest.raises(ValueError, match="twice"):
        parse_register(_SPEC_FIXTURE + "| **OI-15** `[OPEN]` | a second row | ... |\n")


def test_parse_register_rejects_an_untagged_register() -> None:
    with pytest.raises(ValueError, match="tagged register rows"):
        parse_register("| **OI-15** | untagged | ... |")


def test_parse_revision() -> None:
    assert parse_revision(_SPEC_FIXTURE) == "9.9"


def test_dispositioned_counts_as_settled() -> None:
    """R-62(4)'s distinction is preserved in the manifest but not in the rule."""
    assert "DISPOSITIONED" in SETTLED and "RESOLVED" in SETTLED
    assert "OPEN" not in SETTLED


def test_stale_markers_flags_only_markers_not_prose() -> None:
    manifest = Manifest(revision="9.9", issues={"OI-15": "RESOLVED", "OI-17": "OPEN"})
    cites = [
        Citation("OI-15", "a.py", 1, "x = 1  # TBD(OI-15)", is_marker=True),
        Citation("OI-15", "b.py", 2, "# closed by R-62, see OI-15", is_marker=False),
        Citation("OI-17", "c.py", 3, "y = 2  # TBD(OI-17)", is_marker=True),
    ]
    assert [c.path for c in stale_markers(cites, manifest)] == ["a.py"]


def test_stale_markers_flags_a_marker_for_an_unknown_issue() -> None:
    manifest = Manifest(revision="9.9", issues={"OI-17": "OPEN"})
    cites = [Citation("OI-99", "a.py", 1, "# TBD(OI-99)", is_marker=True)]
    assert stale_markers(cites, manifest) == cites


def test_the_exemption_is_exactly_two_files() -> None:
    """The guard's own source and fixtures are outside the scan; nothing else may be.

    Pinned because an exemption that can widen quietly is how a rule stops applying to the
    code it was written for.
    """
    from tools.spec_xref import EXCLUDED

    assert EXCLUDED == {"backend/tools/spec_xref.py", "backend/tests/test_spec_xref.py"}


def test_find_citations_honours_the_exemption(tmp_path: pathlib.Path) -> None:
    from tools import spec_xref

    root = tmp_path / "backend" / "tools"
    root.mkdir(parents=True)
    (root / "spec_xref.py").write_text("# TBD(OI-24)\n", encoding="utf-8")
    assert spec_xref.find_citations(tmp_path, roots=("backend/tools",)) == []


def test_find_citations_skips_a_missing_root(tmp_path: pathlib.Path) -> None:
    assert find_citations(tmp_path, roots=("does/not/exist",)) == []


def test_find_citations_reads_markers_and_prose(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text("a = 1  # TBD(OI-24)\n# see OI-31 for context\n", encoding="utf-8")
    (root / "ignored.md").write_text("OI-24\n", encoding="utf-8")
    found = find_citations(tmp_path, roots=("src",))
    assert [(c.issue, c.line, c.is_marker) for c in found] == [
        ("OI-24", 1, True),
        ("OI-31", 2, False),
    ]


def test_render_report_names_open_issues_and_their_sites() -> None:
    manifest = Manifest(revision="9.9", issues={"OI-15": "RESOLVED", "OI-17": "OPEN"})
    cites = [Citation("OI-17", "a.py", 7, "# OI-17 is why", is_marker=False)]
    out = render_report(cites, manifest)
    assert "OI-17: 1 site(s)" in out
    assert "a.py:7" in out
    assert "OI-15" not in out.split("REVIEW")[1]
