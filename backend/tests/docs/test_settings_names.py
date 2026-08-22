"""Every environment variable the documentation names is a real one (T-705).

`pydantic-settings` sets ``extra="ignore"`` on all 29 groups, which is what makes this worth a
test: a misspelt variable configures nothing, warns about nothing and fails nothing. The stack
comes up healthy, runs on a default, and looks correct -- `docs/CONFIGURATION.md` says so in as
many words, and a typo in the document that says so is the same defect one level up.

**Three oracles, in order, and two of them read the shipped value back.** A token resolves against
the settings model (composed the way `pydantic-settings` composes it), or against the product's own
vocabulary (the OpenAPI enums and ``app.rag.errors``), or against a named allowance. The second is
what makes ``LLM_ERROR`` and ``RETRIEVAL_UNAVAILABLE`` *checked* rather than excused: they are
`FailureClass` members that happen to begin with a settings prefix, so renaming one now fails a
documentation guard -- a second oracle rather than an exemption.

**The allowance is compared against the document's own prose, not merely asserted.**
`docs/CONFIGURATION.md` section 6 lists the settings that deliberately do not exist and the three
that were removed, by name. So :data:`DELIBERATELY_ABSENT` and :data:`REMOVED` are checked against
that list both ways, and `test_no_deliberately_absent_setting_has_quietly_been_implemented` fails
if one of them ever ships.

**What this deliberately does not check:** that every setting is documented.
`tests/test_env_templates.py` already diffs ``backend/.env.example`` against the model in both
directions and boots ``Settings`` from it; a second, weaker copy here would be a liability rather
than coverage.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from tests.docs import BACKEND_ROOT, ascii_only, by_path, documents

# Imported rather than re-derived. Copying the `env_prefix + FIELD.upper()` walk would create the
# second copy that module's own docstring exists to prevent, and its premise test -- which asserts
# the composition rule still holds for every field -- would then protect only one of the two. The
# coupling is deliberate: if those helpers are renamed this fails at collection, which is loud.
from tests.test_env_templates import _group_prefixes, _settings_env_names

_TOKEN: Final = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_UPPER_SNAKE: Final = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

_CONFIGURATION: Final = "docs/CONFIGURATION.md"

#: Names the documentation uses *because* they do not exist. Each absence is a decision, and
#: `docs/CONFIGURATION.md` section 6 carries the argument: an off switch is legitimate only when
#: its off state is a degradation the requirement sanctions.
DELIBERATELY_ABSENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "GATE_ENABLED": "turning off the groundedness gate removes a guarantee (R-48(3)/R-49)",
        "TELEMETRY_ENABLED": "NFR-OBS-01 says shall, so a switch would turn a requirement off",
        "CONTEXT_ENABLED": "its off state deletes FR-STA-04 rather than degrading it (R-51(6))",
        "PARSER_TABLE_ENABLED": "table structure is not a feature flag (R-89)",
        "OCR_BACKEND": "the only alternative vendor class is the one R-88(1) excluded",
    }
)

#: Documented but retired: named in prose so an operator who finds one in an old file knows why it
#: does nothing. Each was wired to nothing, which is the fault the documentation records.
REMOVED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "SSE_PING_SECONDS": "retired at T-405 when the frame envelope changed",
        "EVAL_MAX_CONCURRENCY": "defined, documented and wired to nothing (R-50)",
        "EVAL_BACKEND": "the same dead-setting fault, removed with it",
    }
)

#: Real variables that no `Settings` field reads. Kept separate from
#: `tests.test_env_templates.NOT_SETTINGS` on purpose: only two names overlap, and a shared map
#: would need the union of two staleness rules, so an entry that had gone stale in `.env.example`
#: while still live in the documentation could never be deleted from either.
NOT_A_SETTING: Final[Mapping[str, str]] = MappingProxyType(
    {
        "OCR_LIVE_TEST": "a live-test gate read from os.environ by the suite, not by Settings",
        "KEYCLOAK_LIVE_ADMIN_PASSWORD": "the same, for the live Keycloak tests",
        "MINIO_API_PORT": "a docker-compose host-port interpolation; the app never reads it",
        "MINIO_CONSOLE_PORT": "the same, for the console",
    }
)

_ALLOWANCES: Final = (
    ("deliberately absent", DELIBERATELY_ABSENT),
    ("removed", REMOVED),
    ("not a setting", NOT_A_SETTING),
)


def _vocabulary() -> frozenset[str]:
    """The product's own UPPER_SNAKE strings: OpenAPI enums and consts, plus the failure taxonomy.

    Read back from what ships rather than listed here -- that is the difference between an oracle
    and a second copy.
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and _UPPER_SNAKE.match(node):
            names.add(node)

    walk(json.loads((BACKEND_ROOT / "openapi.json").read_text(encoding="utf-8")))

    from app.rag import errors

    names |= {name for name in dir(errors) if _UPPER_SNAKE.match(name)}
    names |= {member.name for member in errors.FailureClass}
    names |= {member.value for member in errors.FailureClass}
    return frozenset(names)


def _documented_tokens() -> dict[str, set[str]]:
    """Every UPPER_SNAKE token under a real settings prefix, mapped to where it is written.

    Prose *and* code: `GATE_ENABLED` is discussed in backticked prose while `MINIO_API_PORT` only
    ever appears inside a shell command.
    """
    prefixes = tuple(sorted((p for p in _group_prefixes() if p), key=len, reverse=True))
    found: dict[str, set[str]] = {}
    for page in documents():
        for match in _TOKEN.finditer(page.text):
            token = match.group(0)
            if token.startswith(prefixes):
                found.setdefault(token, set()).add(page.path)
    return found


def _section_six_names() -> set[str]:
    """The UPPER_SNAKE names `docs/CONFIGURATION.md` section 6 writes."""
    text = by_path(documents())[_CONFIGURATION].text
    match = re.search(r"^## 6\..*?(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, (
        f"{_CONFIGURATION} section 6 could not be located; this guard needs re-pointing. It reads "
        "the section that names the settings which deliberately do not exist."
    )
    return set(_TOKEN.findall(match.group(0)))


def test_every_documented_variable_under_a_settings_prefix_names_something_real() -> None:
    fields = _settings_env_names()
    vocabulary = _vocabulary()
    allowed = {name for _, table in _ALLOWANCES for name in table}

    unknown: list[str] = []
    for token, where in sorted(_documented_tokens().items()):
        if token in fields or token in vocabulary or token in allowed:
            continue
        unknown.append(f"  {token}  ({', '.join(sorted(where))})")

    assert not unknown, (
        f"{len(unknown)} name(s) in the documentation begin with a real settings prefix and name "
        "nothing that exists:\n" + "\n".join(unknown) + "\n\nEvery settings group sets "
        'extra="ignore", so a misspelt variable configures nothing and reports nothing -- the '
        "stack comes up healthy on a default. Either it is a typo, or it is a product constant "
        "that is no longer exported, or it belongs in one of the allowance maps in "
        "tests/docs/test_settings_names.py with a sentence saying why."
    )


def test_the_deliberately_absent_settings_are_exactly_the_ones_the_configuration_guide_names() -> (
    None
):
    """The allowance is compared against the document's own claim, not against itself.

    A pin whose constant and assertion live in one module is a tautology one edit satisfies. This
    reads `docs/CONFIGURATION.md` section 6 back, so dropping a name from the prose or adding one
    to the map without writing the paragraph both fail.
    """
    fields = _settings_env_names()
    documented = {name for name in _section_six_names() if name not in fields}
    declared = set(DELIBERATELY_ABSENT) | set(REMOVED)

    assert documented == declared, (
        f"{_CONFIGURATION} section 6 and the allowance maps disagree about which settings do not "
        "exist:\n"
        f"  named in the document, not in a map: {sorted(documented - declared)}\n"
        f"  in a map, not named in the document: {sorted(declared - documented)}\n\n"
        "Section 6 is the argument for each absence; the map is what lets the name appear in the "
        "documentation. Neither is complete alone."
    )


def test_no_deliberately_absent_setting_has_quietly_been_implemented() -> None:
    """The direction that bites. Shipping `GATE_ENABLED` must not pass silently."""
    shipped = sorted(set(DELIBERATELY_ABSENT) & set(_settings_env_names()))
    assert not shipped, (
        f"these settings are documented as deliberately absent and now exist: {shipped}\n"
        "Each absence is a ruling -- adding the switch is a specification change, not a "
        f"configuration option. Correct {_CONFIGURATION} section 6 and the map together."
    )


def test_every_variable_allowance_is_still_exercised_and_carries_a_reason() -> None:
    """An allowance nobody uses is a place names go to be forgotten."""
    documented = _documented_tokens()
    stale: list[str] = []
    for label, table in _ALLOWANCES:
        for name, reason in table.items():
            if not reason.strip():
                stale.append(f"  {name} ({label}): allowed with no reason")
            elif name not in documented:
                stale.append(f"  {name} ({label}): allowed, but no document mentions it any more")

    assert not stale, "the variable allowances have drifted from the documentation:\n" + "\n".join(
        sorted(stale)
    )


def test_the_documentation_names_settings_from_across_the_model() -> None:
    """Anti-vacuity: a broken prefix filter or an unread document would otherwise pass green."""
    fields = _settings_env_names()
    documented = _documented_tokens()
    real = {token for token in documented if token in fields}
    groups = {fields[token].split(".")[0] for token in real}

    assert len(real) >= 50, f"only {len(real)} real settings names were found in the documentation"
    assert len(groups) >= 15, (
        f"documented settings span only {len(groups)} groups: {sorted(groups)}"
    )
    assert any(ascii_only(name) == name for name in real)
