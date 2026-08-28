"""`tools.seed_demo` — the demo corpus builders and the contract `plan` prints (T-709).

**No network here.** The HTTP half needs a running API and a worker and is exercised by
running it; what is testable offline is the part that would be wrong silently: the generated
documents, and whether the tool can print its own output on a Windows console.

The document builders deserve tests precisely *because* they are generated rather than
committed. A checked-in fixture is wrong once and visibly; a generator is wrong on the day
someone edits it, and the failure surfaces as "the demo corpus stopped demonstrating the
feature it exists for" — a table PDF with no detectable table, or a "scan" that quietly kept
its text layer and so never exercises recognition at all.
"""

from __future__ import annotations

import csv
import io

import pymupdf
import pytest

from tools import seed_demo


# --- the generated corpus ------------------------------------------------------------


def test_the_report_pdf_really_contains_a_detectable_table() -> None:
    """FR-ING-08's detector must find a grid, or the demo demonstrates nothing.

    Asserted through PyMuPDF's own `find_tables` — the same library the parser uses — rather
    than by looking for the words, which a plain paragraph would also satisfy.
    """
    doc = pymupdf.open(stream=seed_demo._table_pdf(), filetype="pdf")
    try:
        tables = doc[0].find_tables(strategy="lines_strict")
        assert len(tables.tables) >= 1, "no table found; the FR-ING-08 demo document is inert"
        extracted = tables.tables[0].extract()
        header = [cell.strip() if cell else "" for cell in extracted[0]]
        assert header == ["Region", "Revenue (USD)", "Growth"]
        body = {row[0].strip(): row[1].strip() for row in extracted[1:] if row[0]}
        assert body["EMEA"] == "4,812,000", "the seeded answer must be findable in the table"
    finally:
        doc.close()


def test_the_invoice_pdf_has_no_text_layer_at_all() -> None:
    """The scan must be genuinely image-only (R-88(2)).

    If it kept a text layer the parser would read it straight through, the document would
    ingest happily with recognition **off**, and the one document in the set whose whole
    purpose is FR-ING-07 would prove nothing. `get_text` returning empty is the property.
    """
    doc = pymupdf.open(stream=seed_demo._scanned_pdf(), filetype="pdf")
    try:
        assert doc.page_count >= 1
        for page in doc:
            assert page.get_text().strip() == "", "the scan kept a text layer"
            assert page.get_images(), "the scan carries no raster, so there is nothing to read"
    finally:
        doc.close()


def test_the_csv_parses_and_carries_the_answer_its_question_asks_for() -> None:
    rows = list(csv.DictReader(io.StringIO(seed_demo._expenses_csv().decode("utf-8"))))
    assert rows, "empty CSV"
    assert set(rows[0]) == {"category", "quarter", "amount_usd", "owner"}
    travel = [row for row in rows if row["category"] == "Travel"]
    assert travel, "the 'How much was spent on travel?' chat has nothing to retrieve"


def test_the_markdown_carries_a_heading_chain_and_its_answer() -> None:
    """R-34's `section` locator needs nesting, or the FR-CIT-03 card has nothing to name."""
    text = seed_demo._policy_markdown().decode("utf-8")
    assert "# Acme Security Policy" in text
    assert "## Access control" in text
    assert "### Passwords" in text, "no third level, so the locator's chain is trivial"
    assert "rotation policy" in text, "the seeded question would abstain"


def test_every_declared_builder_resolves() -> None:
    """A `SeedDocument` naming a builder that does not exist fails at run time, mid-seed."""
    for doc in seed_demo.DOCUMENTS:
        assert doc.build in seed_demo._BUILDERS, doc.build
        assert seed_demo._build(doc.build), f"{doc.filename} built empty"


def test_exactly_one_document_needs_recognition() -> None:
    """The set is meant to cover the four formats plus one scan; drift here is silent."""
    assert [doc.filename for doc in seed_demo.DOCUMENTS if doc.needs_ocr] == [
        "Acme_Signed_Invoice.pdf"
    ]
    suffixes = sorted({doc.filename.rsplit(".", 1)[1] for doc in seed_demo.DOCUMENTS})
    assert suffixes == ["csv", "md", "pdf"], "FR-ING-02's formats are no longer all represented"


def test_one_seeded_chat_is_expected_to_abstain() -> None:
    """The abstention is deliberate, not an accident of a badly-worded question.

    It is the surface FR-MSG-09's control sits on and the shape `docs/` needs a screenshot of,
    so losing it by "fixing" the question would remove a documented case.
    """
    titles = [chat.title for chat in seed_demo.CHATS]
    assert "Unanswerable" in titles
    unanswerable = next(chat for chat in seed_demo.CHATS if chat.title == "Unanswerable")
    corpus = b" ".join(
        seed_demo._build(doc.build) for doc in seed_demo.DOCUMENTS if doc.build != "scanned_pdf"
    )
    for question in unanswerable.questions:
        for word in ("home", "address"):
            assert word.encode() not in corpus, (
                f"{word!r} appears in the corpus, so this chat may not abstain"
            )


# --- the console it has to print to --------------------------------------------------


def test_everything_the_tool_prints_is_ascii() -> None:
    """R-80's finding, applied before it bites: prose keeps its typography, stdout is ASCII.

    A Windows console is cp1252, and a `UnicodeEncodeError` raised *inside* a print is a crash
    in the reporting path — the failure mode that makes an operator tool useless exactly when
    it is telling you something went wrong. The module docstring is deliberately exempt and
    deliberately **not** passed to argparse, which is what `--help` would otherwise print.
    """
    printed: list[str] = [
        *(doc.why for doc in seed_demo.DOCUMENTS),
        *(doc.filename for doc in seed_demo.DOCUMENTS),
        *(chat.why for chat in seed_demo.CHATS),
        *(chat.title for chat in seed_demo.CHATS),
        *(question for chat in seed_demo.CHATS for question in chat.questions),
    ]
    offenders = [value for value in printed if not value.isascii()]
    assert not offenders, f"these reach stdout and are not ASCII: {offenders}"


def test_argparse_does_not_print_the_module_docstring() -> None:
    """The docstring is typographic prose; `--help` must not be a cp1252 crash."""
    parser_help = _help_text()
    assert parser_help.isascii(), "--help is not ASCII"
    assert "Why it drives the HTTP API" not in parser_help, (
        "the module docstring reached argparse; it contains em dashes"
    )


def _help_text() -> str:
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), pytest.raises(SystemExit):
        seed_demo.main(["--help"])
    return buffer.getvalue()


# --- refusals ------------------------------------------------------------------------


def test_run_refuses_without_yes(capsys: pytest.CaptureFixture[str]) -> None:
    """It creates a user and spends model calls, so it takes `--yes` (R-87(3)'s precedent).

    A flag rather than a prompt: every tool here is scriptable.
    """
    code = seed_demo.main(
        [
            "--base-url",
            "http://127.0.0.1:1",  # never connected to; the guard is reached first
            "--admin-email",
            "admin@corpus.test",
            "--admin-password",
            "x",
            "run",
        ]
    )
    assert code != 0
    assert "--yes" in capsys.readouterr().out
