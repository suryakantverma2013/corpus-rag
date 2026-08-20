"""Render the acceptance manifest for a human (T-606).

The judgement half of the review. ``tests/acceptance/test_completeness.py`` is the hard rule —
every section 9 row carries evidence, every pointer resolves, every NFR carries a disposition —
and this prints the same manifest so that whoever signs the build off can read what covers what,
which is the half no assertion can do for them (``tools/spec_xref.py``'s split, one tool over).

Three sections: the section 9 literal table with its evidence, the section 5 NFR checklist with
its dispositions, and the residual gaps the review could not close. The last one is **generated
from the manifest rather than written down**, so a gap cannot be closed in prose while the
register still carries it.

Exit is always 0. A non-zero exit would invite someone to wire this into CI, where "four
requirements are still open" would read as a build failure rather than as the state of a decision
log (``tools/feedback_calibration.py``'s reason, and the same one).

Stdout is ASCII, and here that takes a transliteration step rather than a convention. R-80(7)'s
lesson was about copy this module controls; the strings below are **data** — section 9's own row
labels carry a section sign and its literals carry em dashes and middle dots — so "just write
ASCII" is not available. ``_ascii`` folds them (``S`` for the section sign, ``-`` for the dashes)
immediately before printing, which keeps every label greppable while guaranteeing that no console
encoding can raise. Printing the raw label was tried first and mangles on a `cp437` terminal.
"""

from __future__ import annotations

import argparse
import json
import sys

from tests.acceptance import (
    NFR_DISPOSITIONS,
    RESIDUAL_GAPS,
    SPEC_9_ROWS,
    Disposition,
    Fidelity,
)

_CLI_DESCRIPTION = (
    "Render the acceptance manifest: the spec's section 9 literal table and section 5 NFR\n"
    "checklist, each mapped to the evidence that covers it, plus the residual gaps.\n"
    "\n"
    "The hard rule lives in tests/acceptance/test_completeness.py; this is the report.\n"
    "Always exits 0 - open requirements are a decision log, not a build failure."
)

#: The only non-ASCII characters section 9's labels and literals actually contain. Anything else
#: falls through to `?` rather than raising, because a report that dies on one row is worse than a
#: report with one unreadable character in it.
_TRANSLIT = str.maketrans(
    {
        "§": "S",  # section sign - "S9 chat copy"
        "—": "-",  # em dash
        "–": "-",  # en dash
        "·": "-",  # middle dot - "PDF - DOCX - CSV - MD"
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
    }
)


def _ascii(text: str) -> str:
    return text.translate(_TRANSLIT).encode("ascii", "replace").decode("ascii")


_DISPOSITION_LABELS = {
    Disposition.MET_BY_TEST: "met by test",
    Disposition.MET_BY_CONSTRUCTION: "met by construction",
    Disposition.ACCEPTED_EXCEPTION: "accepted exception",
    Disposition.OPEN: "OPEN",
}


def render_report(*, verbose: bool) -> str:
    pointers = sum(len(evidence) for evidence in SPEC_9_ROWS.values())
    lines = [
        f"acceptance review - section 9: {len(SPEC_9_ROWS)} rows / {pointers} pointers"
        f" - section 5: {len(NFR_DISPOSITIONS)} requirements"
    ]

    lines.append("")
    lines.append("SECTION 9 - the acceptance-critical literals, and what would fail if one moved")
    for row, evidence in SPEC_9_ROWS.items():
        broken = [item for item in evidence if item.check() is not None]
        live = sum(1 for item in evidence if not isinstance(item, Fidelity))
        mark = "GAP " if broken else "ok  "
        lines.append(f"  {mark}{row}  ({live} in-suite, {len(evidence)} total)")
        for item in evidence:
            reason = item.check()
            if reason is not None:
                lines.append(f"        BROKEN  {reason}")
            elif verbose:
                lines.append(f"        {item.label}")

    lines.append("")
    lines.append("SECTION 5 - NFR dispositions")
    for disposition in Disposition:
        members = [nfr for nfr, row in NFR_DISPOSITIONS.items() if row.disposition is disposition]
        lines.append(f"  {_DISPOSITION_LABELS[disposition]:<20} {len(members):>3}")
        # Every requirement is listed, never only the open ones: the checklist is the
        # deliverable, and a count alone cannot be read as a disposition for any given NFR.
        for nfr in members:
            lines.append(f"      {nfr}  {NFR_DISPOSITIONS[nfr].evidence}")

    lines.append("")
    lines.append(f"RESIDUAL - what the review did not close: {len(RESIDUAL_GAPS)}")
    if RESIDUAL_GAPS:
        lines.append("  Each is filed. An unowned gap is indistinguishable from an oversight.")
        for gap in RESIDUAL_GAPS:
            lines.append(f"  {gap.item}  [{gap.filed_as}]")
            lines.append(f"      {gap.detail}")
    else:
        # The empty state gets its own sentence rather than a caption about a list that is not
        # there. It also says what "empty" does and does not claim: the accepted exceptions above
        # are still exceptions, and re-describing a gap as a feature is the way this number lies.
        lines.append("  Nothing outstanding. Accepted exceptions above are still exceptions -")
        lines.append("  this counts what was filed and left unowned, not what was waived.")

    return _ascii("\n".join(lines))


def _as_json() -> str:
    payload = {
        "section_9": {
            row: {
                "evidence": [item.label for item in evidence],
                "broken": [reason for item in evidence if (reason := item.check()) is not None],
            }
            for row, evidence in SPEC_9_ROWS.items()
        },
        "section_5": {
            nfr: {"disposition": row.disposition.value, "evidence": row.evidence}
            for nfr, row in NFR_DISPOSITIONS.items()
        },
        "residual": [
            {"item": gap.item, "detail": gap.detail, "filed_as": gap.filed_as}
            for gap in RESIDUAL_GAPS
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.acceptance",
        # Not `__doc__`: the module docstring is prose and may carry characters a cp1252
        # console cannot encode, and argparse writes `description` straight to stdout.
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verbose", action="store_true", help="list every evidence pointer, not just the gaps"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    print(_as_json() if args.json else render_report(verbose=args.verbose))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
