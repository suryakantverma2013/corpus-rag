"""The spec §11 critical production scenarios, automated (T-601).

§11 is a twelve-row table of *acceptance* behaviours — how the platform behaves under
concurrency and failure — lifted from `AdditionalChatBotRequirements.md §13`, whose lead-in
is normative for this package: *"Your solution should explicitly test these cases."*

**Why this package exists when the components are already tested.** Everything here has
component coverage elsewhere, and that coverage is good. What it does not have is a single
test that crosses the **route → worker → route** boundary: before T-601,
`workers.ingest.ingest_document` was imported by exactly one test module, which seeded its
documents by direct row insert, and `tests/test_chat_live.py` says in its own docstring that
it seeds `document_chunks` rows directly and so exercises neither parser, chunker nor worker.
§11 rows 3–6 are precisely the interleavings that only exist across that boundary: row 1's
existing test proves the API answers `200 duplicate:true`, and says nothing about whether a
second ingestion ran.

So these tests drive the real entry points, inject a real fault, and assert the property the
row names. They are deliberately the slowest tests in the suite.

**Four rows do not describe this system literally, and R-76 (§8.65) fixes the reading.**
Rows 6 and 11 name a vector store that can fail independently of the database — the upstream
source said *Milvus* — but R-16 put vectors in **pgvector inside Postgres**, so the two writes
share one transaction and the split cannot occur as worded. Row 7 asks for a "controlled
re-embedding job" that `JobType` does not have. Row 8 says a user *loses document permission*
where OI-15's global-per-user scope means permission is never granted and so never revoked.
Each test's docstring states the translation it relies on; R-76 is where it is ruled.

`SPEC_11_ROWS` is the committed manifest, on the R-57(2)/T-607 precedent: the spec is
gitignored, so a test that parsed §11 directly would **skip** in the public repo, and a test
that has only ever skipped is not a passing test. The hard rule — every row has a test — runs
everywhere against this dict; `test_completeness.py` adds the spec-gated drift check that the
dict still matches the table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

__all__ = ["SPEC_11_ROWS", "scenario", "spec_11_row_of"]

#: The §11 table's first column, verbatim, keyed by a stable id. The text is the drift
#: check's subject: `test_completeness.py` reads the spec, when it is present, and fails if
#: a row's wording moved or a thirteenth row appeared.
SPEC_11_ROWS: dict[str, str] = {
    "S01": "Same file uploaded twice",
    "S02": "Modified file uploaded",
    "S03": "Delete while ingestion is running",
    "S04": "Query during deletion",
    "S05": "Worker crashes after vector insertion",
    "S06": "DB succeeds but vector store fails",
    "S07": "Embedding model changes",
    "S08": "User loses document permission",
    "S09": "Poisoned document instructions",
    "S10": "Low groundedness",
    "S11": "Vector store unavailable",
    "S12": "Partial parsing failure",
}

#: The attribute `scenario` stamps onto a test function. Read by `test_completeness.py`.
_ROW_ATTR = "__spec_11_row__"

F = TypeVar("F", bound=Callable[..., object])


def scenario(row_id: str) -> Callable[[F], F]:
    """Stamp a test with the §11 row it discharges.

    A decorator rather than a naming convention because the id has to be *readable back*:
    the completeness guard collects stamps and fails when a row has none. Cites an unknown
    id and this raises at import, so a typo cannot silently leave a row uncovered while
    looking covered.
    """
    if row_id not in SPEC_11_ROWS:
        raise ValueError(f"{row_id!r} is not a §11 row; known rows: {sorted(SPEC_11_ROWS)}")

    def decorate(func: F) -> F:
        setattr(func, _ROW_ATTR, row_id)
        return func

    return decorate


def spec_11_row_of(obj: object) -> str | None:
    """The §11 row `obj` was stamped with, or `None`. The guard's reader."""
    row = getattr(obj, _ROW_ATTR, None)
    return row if isinstance(row, str) else None
