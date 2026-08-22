"""`docs/HTTP_API.md` must keep matching `backend/openapi.json` (T-703).

This is the third link in a chain that already exists, and it is only safe because the first two
are guarded too:

    the running app  ->  backend/openapi.json  ->  docs/HTTP_API.md
                     (test_openapi_contract)   (here)

`docs/` is the only documentation the public repository ships, so the HTTP surface is committed as
readable Markdown rather than left as raw JSON. Committing a *generated* file is exactly the stale
artefact the rest of T-703 avoids by gitignoring its output -- the trade is paid for by this
module, and if these tests are ever deleted the file should be gitignored the same day.

Two of the four tests exist to stop the guard being vacuous rather than to check the product:
an empty renderer compared against an empty file passes, and a renderer that varies run to run
fails on an unchanged tree. Both are the failure mode where a green test certifies nothing.
"""

from __future__ import annotations

import re

import pytest
from tools.httpdocs import OUTPUT_PATH, SPEC_PATH, _load, _operations, render

_STALE = f"""\
{OUTPUT_PATH.name} is out of date with {SPEC_PATH.name}.

Regenerate it:

    cd backend && uv run python -m tools.httpdocs

It is committed on purpose -- see the module docstring in tools/httpdocs.py -- so the
regenerated file belongs in the same commit as whatever changed the API.
"""


@pytest.fixture(scope="module")
def spec() -> dict:
    return _load()


@pytest.fixture(scope="module")
def rendered(spec: dict) -> str:
    return render(spec)


def test_the_committed_contract_matches_the_specification(rendered: str) -> None:
    assert OUTPUT_PATH.exists(), f"{OUTPUT_PATH} is missing entirely.\n\n{_STALE}"
    assert OUTPUT_PATH.read_text(encoding="utf-8") == rendered, _STALE


def test_the_renderer_is_deterministic(spec: dict, rendered: str) -> None:
    """Render twice and compare.

    The guard above compares bytes, so anything that varied between runs -- a timestamp, an
    unsorted mapping -- would make it fail against an unchanged tree and train whoever hits it
    to regenerate on reflex. That is worse than no guard, because it survives real drift.
    """
    assert render(spec) == rendered


def test_the_rendered_contract_is_substantive(spec: dict, rendered: str) -> None:
    """Anti-vacuity: a renderer returning nothing would satisfy the equality test.

    Assertions are about *shape*, not about a pinned count. A pinned total would make every new
    endpoint touch a number in a test that has no opinion about endpoints (§8.77(6)).
    """
    operations = _operations(spec)
    assert len(operations) > 20, "suspiciously few operations -- is the specification truncated?"

    for method, path, op in operations:
        assert f"### {method} {path}" in rendered, f"{method} {path} is missing from the contract"
        assert op["operationId"] in rendered

    for name in spec["components"]["schemas"]:
        assert f"### `{name}`" in rendered, f"schema {name} is missing from the contract"


def test_every_internal_anchor_resolves(rendered: str) -> None:
    """Every `](#foo)` has a matching `<a id="foo">`, and no anchor is defined twice.

    Markdown does not complain about a link into nowhere, so without this the index quietly
    rots as operation ids change. T-705 generalises this across `docs/`; the contract carries
    its own because it is the one file here that is machine-written.
    """
    defined = re.findall(r'<a id="([^"]+)"></a>', rendered)
    linked = set(re.findall(r"\]\(#([^)]+)\)", rendered))

    duplicates = {a for a in defined if defined.count(a) > 1}
    assert not duplicates, f"anchors defined more than once: {sorted(duplicates)}"
    assert not linked - set(defined), f"links to nowhere: {sorted(linked - set(defined))}"
