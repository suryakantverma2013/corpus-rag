"""The numbers the documentation states about the configuration surface are the real ones (T-705).

**Why this is not the pinned count the other guards refuse.** `test_env_templates.py` declines to
assert its own figures, and the reason it gives is exact: "constant and assertion would live in one
module, so the pin is a tautology a single edit satisfies". Here the claim is a sentence in
`docs/CONFIGURATION.md` and the value is the shape of `app/config.py` -- two files, two authors,
two commits. Comparing them reads the shipped value back, which is the second-oracle property
`tests/acceptance/`'s `Default`, `Constant` and `Vocabulary` pointers exist for.

And the failure it prevents has already happened: `test_env_templates.py`'s own docstring records
these figures reading 159 / 28 / 32 for a whole release, because Rev 0.55 added settings and nobody
re-counted prose that nothing asserted. That is one release of a document telling a reader the
wrong number about the file it exists to explain.

**Test counts are deliberately not here.** Computing "2,046 collected" means collecting the suite
inside the suite, and how many of those run depends on what is up -- `docs/TESTING.md` says so
itself and tells the reader to run it rather than trust the table. A number with no oracle stays
narrative.

The second test is the one that keeps the first honest: a claim whose wording changes must fail
loudly rather than drop out of the checked set and leave a stale number nobody is comparing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from tests.docs import BACKEND_ROOT, by_path, documents
from tests.test_env_templates import (
    _compose_container_env,
    _settings_env_names,
    _settings_groups,
)

#: What each claim can be measured against. The same walks `test_env_templates.py` diffs the
#: templates with, so a documented number and a guarded template can never disagree.
ORACLES: Final[Mapping[str, Callable[[], int]]] = MappingProxyType(
    {
        "composed settings names": lambda: len(_settings_env_names()),
        "settings groups": lambda: len(_settings_groups()),
        "container environment keys": lambda: len(_compose_container_env()),
        "migration revisions": lambda: len(_migration_revisions()),
        "acceptance section-9 rows": lambda: len(_acceptance_rows()),
        "documentation manifest entries": lambda: len(_documents()),
    }
)


def _documents() -> tuple[object, ...]:
    """Every published markdown file, from the manifest tests/docs/ already reconciles
    against the tree in both directions - so this counts the set the guard enforces, not a
    second list that could disagree with it."""
    from tests.docs import DOCUMENTS

    return DOCUMENTS


def _acceptance_rows() -> Mapping[str, object]:
    """The section-9 manifest, imported rather than counted from the spec.

    The spec is gitignored, so it cannot be the oracle in a public checkout -- but
    `tests/acceptance/` guards the manifest against section 9 both ways and in order, so the
    manifest is the same number by a route that works everywhere.
    """
    from tests.acceptance import SPEC_9_ROWS

    return SPEC_9_ROWS


def _migration_revisions() -> list[str]:
    """Every Alembic revision on disk, by filename.

    Counted from the directory rather than by walking `down_revision` links: the claim the
    document makes is "this many revisions exist", and a chain walk would also fail for reasons
    (a branch, a missing parent) that have nothing to do with the sentence being checked.
    """
    versions = BACKEND_ROOT / "app" / "db" / "migrations" / "versions"
    return sorted(path.name for path in versions.glob("*.py") if path.name != "__init__.py")


@dataclass(frozen=True, slots=True)
class Claim:
    """A number a document states about something this suite can count.

    ``pattern`` has exactly one capturing group -- the number -- and every match of it in that
    document must equal the oracle, so a figure repeated in two sentences is checked in both.
    """

    doc: str
    pattern: str
    oracle: str
    about: str


CLAIMS: Final[tuple[Claim, ...]] = (
    Claim(
        "docs/CONFIGURATION.md",
        r"documents all (\d+) of them",
        "composed settings names",
        "the number of settings backend/.env.example documents",
    ),
    Claim(
        "docs/CONFIGURATION.md",
        r"(\d+) names, types, defaults",
        "composed settings names",
        "the number of names app/config.py declares",
    ),
    Claim(
        "docs/CONFIGURATION.md",
        r"(\d+) groups, each with an `env_prefix`",
        "settings groups",
        "the number of settings groups",
    ),
    Claim(
        "docs/CONFIGURATION.md",
        r"a container actually receives\*\* — (\d+) keys",
        "container environment keys",
        "the number of keys x-corpus-env forwards",
    ),
    Claim(
        "docs/DATA_MODEL.md",
        r"(\d+) revisions, oldest first",
        "migration revisions",
        "the number of Alembic revisions on disk",
    ),
    Claim(
        "docs/ACCEPTANCE.md",
        r"literal values\*\* — (\d+) rows",
        "acceptance section-9 rows",
        "the number of section-9 rows the acceptance manifest covers",
    ),
    Claim(
        "docs/MODULE_MAP.md",
        r"\((\d+)-document manifest\)",
        "documentation manifest entries",
        "the number of published documents tests/docs/ reconciles against the tree",
    ),
    Claim(
        "backend/README.md",
        r"(\d+) groups, ~\d+ composed names",
        "settings groups",
        "the number of settings groups",
    ),
    Claim(
        "backend/README.md",
        r"\d+ groups, ~(\d+) composed names",
        "composed settings names",
        "the number of names app/config.py declares",
    ),
)


def _matches(claim: Claim) -> list[tuple[int, int]]:
    """`(line, number)` for every place the claim is made in its document."""
    text = by_path(documents())[claim.doc].text
    found: list[tuple[int, int]] = []
    for match in re.finditer(claim.pattern, text):
        line = text.count("\n", 0, match.start()) + 1
        found.append((line, int(match.group(1))))
    return found


def test_every_computable_numeric_claim_matches_the_thing_it_counts() -> None:
    wrong: list[str] = []
    for claim in CLAIMS:
        actual = ORACLES[claim.oracle]()
        for line, stated in _matches(claim):
            if stated != actual:
                wrong.append(
                    f"  {claim.doc}:{line} says {stated}; {claim.oracle} is now {actual}"
                    f"  ({claim.about})"
                )

    assert not wrong, (
        f"{len(wrong)} documented number(s) no longer match what they count:\n"
        + "\n".join(sorted(wrong))
        + "\n\nUpdate the sentence. These are counted by the same walks "
        "tests/test_env_templates.py uses to diff the templates, so the documentation and the "
        "guarded templates cannot disagree -- but the prose can, and has, for a whole release."
    )


def test_every_numeric_claim_is_still_made_in_the_document_that_makes_it() -> None:
    """Anti-vacuity, and the more important half.

    A reworded sentence stops matching, the claim silently leaves the checked set, and the number
    it left behind rots exactly as before. This makes rewording a test failure, which is a cheap
    prompt to re-point the pattern at the new sentence.
    """
    silent = [
        f"  {claim.doc}: /{claim.pattern}/ matches nothing ({claim.about})"
        for claim in CLAIMS
        if not _matches(claim)
    ]
    assert not silent, (
        f"{len(silent)} numeric claim(s) are no longer written where this guard looks:\n"
        + "\n".join(silent)
        + "\n\nEither the sentence was reworded -- re-point the pattern in tests/docs/"
        "test_claims.py -- or the claim was dropped, in which case drop the Claim with it."
    )


def test_every_oracle_is_used_and_counts_something() -> None:
    used = {claim.oracle for claim in CLAIMS}
    assert used == set(ORACLES), f"unused oracles: {sorted(set(ORACLES) - used)}"
    for name, count in ORACLES.items():
        assert count() > 0, f"the {name} oracle counts nothing; it is measuring the wrong thing"


def _mapped_tables() -> frozenset[str]:
    """Every table the ORM declares, read off the metadata rather than off a list here.

    A list would be the tautology this module's docstring refuses -- it would have to be edited
    by whoever adds the table, which is exactly the person who forgot the document.
    """
    from app.db import models  # noqa: F401  -- importing is what populates the metadata
    from app.db.base import Base

    return frozenset(Base.metadata.tables)


def _data_model_section(heading: str) -> str:
    """The body of one `## ` section of the data-model document, heading excluded."""
    text = by_path(documents())["docs/DATA_MODEL.md"].text
    start = text.find(f"## {heading}")
    assert start != -1, f"docs/DATA_MODEL.md has no section {heading!r}; re-point this guard"
    body = text[start + len(f"## {heading}") :]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def test_every_table_the_orm_declares_is_described_in_the_data_model() -> None:
    """The gap that made this guard worth writing, and it had gone unnoticed twice.

    `docs/DATA_MODEL.md` section 3 documented neither `model_overrides` (shipped in T-611, Rev
    0.50) nor `document_figures` (T-714) -- one of them for eight revisions -- while the document
    presents itself as the description of the schema. Nothing could see it: the section is prose,
    the link and section guards only check that references resolve, and a table nobody mentions
    breaks no reference.

    **Scoped to section 3, and the first draft of this guard was not.** Searching the whole
    document passed with the section-3 paragraph deleted, because section 10's migration table
    names the table too -- so the check would have gone green on exactly the defect it is named
    for, the moment anyone listed the migration. A mutation is what showed it. The section slice
    is anchored on the heading alone, which the numbered-heading guard already pins, so an
    editorial reflow inside the section still passes.
    """
    tables = _data_model_section("3. Tables")
    missing = sorted(name for name in _mapped_tables() if f"`{name}`" not in tables)
    assert not missing, (
        f"{len(missing)} table(s) exist in the schema and are described nowhere in "
        "docs/DATA_MODEL.md section 3:\n  " + "\n  ".join(missing) + "\n\nAdd each one. A table "
        "the data-model document does not describe is one a reader cannot discover except by "
        "reading migrations -- which is how model_overrides went undocumented for eight "
        "revisions and document_figures for its whole life until T-717."
    )


#: `### `app/services/` — 21 modules`, and the `app/db/` form that also sizes its subpackages.
_PACKAGE_HEADING: Final = re.compile(r"^### `([a-z_][a-z_/]*/)` \u2014 (.+)$", re.M)
_SUBPACKAGE: Final = re.compile(r"`(\w+)/` \((\d+)\)")


def _package_modules(relative: str) -> int | None:
    """Importable modules in one backend package, `__init__.py` excluded."""
    directory = BACKEND_ROOT / relative
    if not directory.is_dir():
        return None
    return len([path for path in directory.glob("*.py") if path.name != "__init__.py"])


def test_every_module_count_in_the_map_matches_the_package_it_names() -> None:
    """The same rot as the migration list, one layer over, and it had four live instances.

    `docs/MODULE_MAP.md` sizes every package in its heading. Four were wrong when T-717 looked --
    `app/ingestion/` and its `parsers/`, `app/services/`, and `app/db/repositories/` -- because
    T-713..T-716 added a module to each and the headings are prose that nothing counted.

    Generic rather than a list of `Claim`s on purpose: the oracle is "count the directory the
    heading already names", so a new package guards itself the moment someone documents it, and
    there is no second list to keep in step with the first.
    """
    text = by_path(documents())["docs/MODULE_MAP.md"].text
    checked = 0
    wrong: list[str] = []

    for heading in _PACKAGE_HEADING.finditer(text):
        package, rest = heading.group(1), heading.group(2)
        line = text.count("\n", 0, heading.start()) + 1

        sized = re.search(r"(\d+) modules", rest)
        for name, stated in ((package, sized.group(1)),) if sized else ():
            checked += 1
            actual = _package_modules(name)
            if actual != int(stated):
                wrong.append(f"  MODULE_MAP.md:{line} says {name} has {stated}; it has {actual}")

        for sub, stated in _SUBPACKAGE.findall(rest):
            checked += 1
            actual = _package_modules(f"{package}{sub}")
            if actual != int(stated):
                wrong.append(
                    f"  MODULE_MAP.md:{line} says {package}{sub}/ has {stated}; it has {actual}"
                )

    assert checked >= 12, (
        f"only {checked} package size(s) found in docs/MODULE_MAP.md -- the heading format "
        "changed and this guard is now measuring almost nothing; re-point _PACKAGE_HEADING."
    )
    assert not wrong, (
        f"{len(wrong)} package size(s) in the module map no longer match the tree:\n"
        + "\n".join(sorted(wrong))
        + "\n\nUpdate the heading, and list the new module beside its siblings on the line under "
        "it -- a map that is one module behind is worse than none, because it reads as complete."
    )
