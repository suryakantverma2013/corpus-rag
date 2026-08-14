"""The guard that makes "all 12 §11 scenarios automated" checkable rather than asserted.

T-601's deliverable is the *whole table*. Without a guard, a row can be dropped in review, or
a scenario can be deleted in a later refactor, and the suite stays green while the claim on
the board quietly stops being true — the same drift `tests/test_harness.py` and
`tools/spec_xref.py` exist to make impossible.

Two checks, on the R-57(2)/T-607 split:

* **The hard rule runs everywhere**, against the committed `SPEC_11_ROWS` manifest. The spec
  is gitignored, so a check that parsed §11 directly would `skip` in the public repo — and a
  test that has only ever skipped is not a passing test.
* **The drift check is spec-gated.** When the spec is on disk it re-reads the table and fails
  if a row's wording moved or a thirteenth row appeared, which is the only thing that can
  make the manifest stale.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import re

import pytest

import tests.scenarios
from tests.scenarios import SPEC_11_ROWS, spec_11_row_of

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "Nexus_AI_Detailed_Specification.md"

requires_spec = pytest.mark.skipif(
    not SPEC_PATH.is_file(),
    reason="Nexus_AI_Detailed_Specification.md is gitignored; the manifest carries the table",
)


def _covered_rows() -> dict[str, list[str]]:
    """Every §11 row id stamped anywhere in this package, mapped to the tests carrying it."""
    covered: dict[str, list[str]] = {}
    for info in pkgutil.iter_modules(tests.scenarios.__path__):
        if not info.name.startswith("test_"):
            continue
        module = importlib.import_module(f"{tests.scenarios.__name__}.{info.name}")
        for attribute in vars(module).values():
            row = spec_11_row_of(attribute)
            if row is not None:
                covered.setdefault(row, []).append(f"{info.name}::{attribute.__name__}")
    return covered


def test_every_section_11_row_has_a_test() -> None:
    """The deliverable, as an assertion.

    Deleting any scenario below fails this by name — verified by mutation, not assumed.
    """
    covered = _covered_rows()
    missing = sorted(set(SPEC_11_ROWS) - set(covered))
    assert not missing, "§11 rows with no automated scenario: " + ", ".join(
        f"{row} ({SPEC_11_ROWS[row]!r})" for row in missing
    )


def test_no_test_claims_a_row_that_does_not_exist() -> None:
    """The other direction: a stamp must cite a real row.

    `scenario()` already raises at import on an unknown id, so this is the guard on the
    guard — it fails if that check is ever relaxed to a warning.
    """
    unknown = sorted(set(_covered_rows()) - set(SPEC_11_ROWS))
    assert not unknown, f"tests stamped with unknown §11 rows: {unknown}"


@requires_spec
def test_the_manifest_still_matches_the_specs_table() -> None:
    """Drift: §11 is the source, `SPEC_11_ROWS` is the committed copy.

    Reads the table's first column and compares it to the manifest as an ordered list, so a
    reworded row, a new row and a deleted row all fail — and each of them should, because
    each changes what T-601 owes.
    """
    text = SPEC_PATH.read_text(encoding="utf-8")
    section = re.search(r"^## 11\. Critical Production Scenarios.*?(?=^## )", text, re.S | re.M)
    assert section, "§11 not found in the spec; this guard needs re-pointing"

    triggers = [
        line.split("|")[1].strip()
        for line in section.group(0).splitlines()
        if line.startswith("|") and not line.startswith("|---")
    ]
    # Drop the header row (`| Scenario | Expected behaviour |`).
    triggers = [trigger for trigger in triggers if trigger != "Scenario"]

    assert triggers == list(SPEC_11_ROWS.values()), (
        "§11 has moved under the manifest.\n"
        f"spec:     {triggers}\n"
        f"manifest: {list(SPEC_11_ROWS.values())}"
    )


@requires_spec
def test_the_table_still_has_exactly_twelve_rows() -> None:
    """The board says twelve. If the spec ever says otherwise, that is a task-scope change."""
    assert len(SPEC_11_ROWS) == 12
