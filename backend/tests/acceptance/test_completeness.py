"""The guards that make "§9 and §5 are satisfied" checkable rather than asserted.

Four questions, in the order they can go wrong:

1. does every §9 row carry evidence at all?
2. does that evidence still **resolve** — the one with teeth, because a deleted test, a renamed
   constant or a reworded literal all fail here by name;
3. does every §5 requirement carry a disposition, and does an ``OPEN`` one say what is open?
4. has the spec moved under the manifest?

(1)–(3) run everywhere, against the committed manifest. (4) is spec-gated: the spec is gitignored,
so a check that parsed it directly would ``skip`` in the public repo — and a test that has only
ever skipped is not a passing test (R-57(2), Rev 0.12's lesson).

The cross-package guard is worth its own line. ``tests/security/__init__.py`` already carries
§5.2's eight ids and the two it defers; this file asserts the two registers agree, so §5.2 has one
owner rather than two copies that drift.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.acceptance import NFR_DISPOSITIONS, SPEC_9_ROWS, Disposition
from tests.security import DEFERRED_NFRS, NFR_SEC_ROWS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "Nexus_AI_Detailed_Specification.md"

requires_spec = pytest.mark.skipif(
    not SPEC_PATH.is_file(),
    reason="Nexus_AI_Detailed_Specification.md is gitignored; the manifest carries the tables",
)


def _section(heading: str) -> str:
    text = SPEC_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{heading}.*?(?=^## )", text, re.S | re.M)
    assert match, f"{heading} not found in the spec; this guard needs re-pointing"
    return match.group(0)


# --- (1)–(2) the §9 table ----------------------------------------------------------------


def test_every_section_9_row_carries_evidence() -> None:
    """The deliverable, as an assertion: no acceptance-critical literal is unowned."""
    bare = [row for row, evidence in SPEC_9_ROWS.items() if not evidence]
    assert not bare, "§9 rows with no evidence: " + ", ".join(bare)


def test_every_evidence_pointer_resolves() -> None:
    """The guard with teeth.

    Each pointer answers with a reason or with None, so one run reports every break rather than
    stopping at the first — an acceptance guard that hides the second failure is the wrong shape.
    """
    broken = [
        f"{row}: {reason}"
        for row, evidence in SPEC_9_ROWS.items()
        for reason in (item.check() for item in evidence)
        if reason is not None
    ]
    assert not broken, "§9 evidence that no longer resolves:\n  " + "\n  ".join(broken)


def test_no_row_leans_on_the_fidelity_harness_alone() -> None:
    """A row whose only evidence needs a browser, a dev server and a live login is not covered.

    The harness is the right instrument for rendered geometry and it runs on demand, not in CI —
    so a row with nothing else has no evidence in any run anybody performs routinely.
    """
    from tests.acceptance import Fidelity

    alone = [
        row
        for row, evidence in SPEC_9_ROWS.items()
        if evidence and all(isinstance(item, Fidelity) for item in evidence)
    ]
    assert not alone, "§9 rows evidenced only by the on-demand harness: " + ", ".join(alone)


# --- (3) the NFR checklist ---------------------------------------------------------------


def test_every_nfr_carries_a_disposition() -> None:
    missing = [nfr for nfr, row in NFR_DISPOSITIONS.items() if not row.evidence.strip()]
    assert not missing, "NFRs with an empty evidence line: " + ", ".join(missing)


def test_an_open_disposition_names_what_is_open() -> None:
    """`OPEN` with no referent is how a question stops being findable.

    Every open row must name the marker or the ruling that owns it, which is what lets the
    residual-gap list in `docs/ACCEPTANCE.md` be generated rather than remembered.
    """
    unreferenced = [
        nfr
        for nfr, row in NFR_DISPOSITIONS.items()
        if row.disposition is Disposition.OPEN
        and "(D)" not in row.evidence
        and "§8.4" not in row.evidence
    ]
    assert not unreferenced, "open NFRs naming nothing: " + ", ".join(unreferenced)


def test_the_security_nfrs_agree_with_the_security_package() -> None:
    """§5.2 has one owner. Two registers that disagree is worse than one that is wrong.

    **The relation is containment, not equality, and R-86(1) is why.** The two registers answer
    different questions: `DEFERRED_NFRS` says *this package has no request-path surface to drive*,
    while an `OPEN` disposition says *nothing anywhere covers it*. Those coincided until the realm
    gained a password policy — NFR-SEC-04 is now guarded offline in `tests/test_account_linking.py`
    and is still not the security package's job. Equality would have forced a false choice between
    marking a covered requirement open and claiming a deferral the package cannot honour.
    """
    here = {nfr for nfr in NFR_DISPOSITIONS if nfr.startswith("NFR-SEC-")}
    assert here == set(NFR_SEC_ROWS), (
        f"§5.2 ids disagree — acceptance {sorted(here)}, security {sorted(NFR_SEC_ROWS)}"
    )
    assert set(DEFERRED_NFRS) <= here, (
        f"the security package defers ids §5.2 does not have: {sorted(set(DEFERRED_NFRS) - here)}"
    )
    open_here = {nfr for nfr in here if NFR_DISPOSITIONS[nfr].disposition is Disposition.OPEN}
    orphaned = open_here - set(DEFERRED_NFRS)
    assert not orphaned, (
        f"open here but not deferred there — one of the two is wrong: {sorted(orphaned)}"
    )


# --- (4) drift against the spec ----------------------------------------------------------


@requires_spec
def test_the_manifest_still_matches_section_9() -> None:
    """§9 is the source, `SPEC_9_ROWS` is the committed copy.

    Compared as an ordered list, so a new row, a reworded row and a deleted row all fail — each
    of them should, because each changes what acceptance owes.
    """
    rows = [
        line.split("|")[1].strip()
        for line in _section(r"## 9\. Acceptance-Critical Literal Values").splitlines()
        if line.startswith("|") and not line.startswith("|---")
    ]
    rows = [row for row in rows if row != "Item"]  # the header

    assert rows == list(SPEC_9_ROWS), (
        f"§9 has moved under the manifest.\nspec:     {rows}\nmanifest: {list(SPEC_9_ROWS)}"
    )


@requires_spec
def test_the_nfr_ids_still_match_section_5() -> None:
    """Only the first id on a requirement line counts — a struck-through heading names its own
    id twice (`~~**NFR-OBS-04 (D)**~~ **NFR-OBS-04**`) and that is one requirement, not two."""
    ids: list[str] = []
    for line in _section(r"## 5\. Non-Functional Requirements").splitlines():
        if not line.startswith("- "):
            continue
        found = re.search(r"NFR-[A-Z0-9]+-\d+", line)
        if found:
            ids.append(found.group(0))

    assert ids == list(NFR_DISPOSITIONS), (
        f"§5 has moved under the manifest.\nspec:     {ids}\nmanifest: {list(NFR_DISPOSITIONS)}"
    )
