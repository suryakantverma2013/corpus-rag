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

from tests.docs import by_path, documents
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
    }
)


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
