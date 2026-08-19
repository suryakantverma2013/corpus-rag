"""The report half's own guards (T-606).

Three properties, and the first is the one that has bitten this repository twice: **stdout is
ASCII**. R-80(7)'s `cp1252` console raised on a thumb emoji; here the hazard is data rather than
copy — section 9's row labels carry a section sign — so the fold is asserted rather than trusted.

The other two are that the report is complete (a row or a requirement that never reaches the page
is invisible exactly when someone is signing the build off) and that it always exits 0.
"""

from __future__ import annotations

import json

from tools.acceptance import main, render_report

from tests.acceptance import NFR_DISPOSITIONS, RESIDUAL_GAPS, SPEC_9_ROWS


def test_the_report_is_pure_ascii() -> None:
    """Asserted on the rendered page, not on the source, because the risk is in the data."""
    report = render_report(verbose=True)
    report.encode("ascii")  # raises on the first character a cp1252 console would reject
    assert "§" not in report


def test_every_row_and_requirement_reaches_the_page() -> None:
    report = render_report(verbose=False)
    for row in SPEC_9_ROWS:
        assert row.replace("§", "S") in report, f"{row} is missing from the report"
    for nfr in NFR_DISPOSITIONS:
        assert nfr in report, f"{nfr} is missing from the report"
    for gap in RESIDUAL_GAPS:
        assert gap.item in report, f"the {gap.item} gap is missing from the report"


def test_the_verbose_form_names_the_evidence_and_the_summary_does_not() -> None:
    """Two audiences: a reader checking coverage, and a reader chasing one row's evidence."""
    summary = render_report(verbose=False)
    verbose = render_report(verbose=True)
    needle = "tests/test_jobs_api.py::test_a_foreign_job_is_404"
    assert needle in verbose
    assert needle not in summary
    assert len(verbose) > len(summary)


def test_the_json_form_carries_the_whole_manifest() -> None:
    payload = json.loads(_capture(["--json"]))
    assert set(payload["section_9"]) == set(SPEC_9_ROWS)
    assert set(payload["section_5"]) == set(NFR_DISPOSITIONS)
    assert len(payload["residual"]) == len(RESIDUAL_GAPS)
    assert not any(row["broken"] for row in payload["section_9"].values())


def test_it_always_exits_zero() -> None:
    """Open requirements are a decision log, not a build failure.

    A non-zero exit would invite someone to wire this into CI, where "four requirements are still
    open" would fail the build for reporting the truth.
    """
    assert main([]) == 0
    assert main(["--verbose"]) == 0
    assert main(["--json"]) == 0


def _capture(argv: list[str]) -> str:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()
