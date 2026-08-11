"""Cross-reference the spec's open-issue register against the code that cites it (T-607).

**The problem this exists to stop is a decision being made twice.** Three consecutive
revisions ruled on something that was already implemented and simply never written down:
R-61's browser floor (the answer was Vite's ``build.target``, enforced on every build since
T-004), R-62's OI-23 and OI-25 closures (both already true in the code — ``app/rag/state.py``
even cited OI-23 in its docstring), and R-67's context-overflow copy (a ``# TBD`` string that
*was* the ruling, filed under a marker because the issue above it was open). The register
rots in the other direction too: 11 provisional ``TBD`` markers naming the multi-tenancy
issue survived R-62(4)'s disposition of it, and every one of them polluted the ``# TBD``
grep CLAUDE.md points a new session at.

*(This file and its test are in ``EXCLUDED`` below: both must quote marker syntax verbatim to
state and to test the rule, and a textual convention cannot distinguish a quoted marker from
a real one. A real limitation, named rather than worked around.)*

Two outputs, deliberately different in kind:

* a **hard rule** — a ``TBD(OI-NN)`` marker may only cite an issue that is still open. That
  is objective, so it is a failing test (``tests/test_spec_xref.py``) rather than advice.
* a **review report** — for each open issue, every code site that mentions it. Nothing can
  be asserted about these; the point is that a human about to rule on an issue reads what
  the tree already says about it first. This is the half that would have caught all three
  instances above.

**Why a committed manifest rather than parsing the spec at test time.** The spec is
gitignored (CLAUDE.md housekeeping — it is not ours to publish), so a test that parsed it
would skip in the public repo, and *a test that has only ever skipped is not a passing test*
(Rev 0.12's lesson). So the statuses are extracted into ``spec_issues.json``, which is
committed and carries no spec prose — only issue ids and a one-word status. The hard rule
runs everywhere against the manifest; a second check, gated on the spec being present,
fails when the manifest and the register disagree. This is **R-57(2)'s pattern** applied to
a second generated artifact: commit the derived file, and test that it still matches its
source.

Usage, from ``backend/``::

    uv run python -m tools.spec_xref            # print the report; exit 1 on a hard-rule breach
    uv run python -m tools.spec_xref --write    # regenerate spec_issues.json from the spec
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass

__all__ = [
    "Citation",
    "MANIFEST_PATH",
    "SETTLED",
    "SPEC_PATH",
    "STATUSES",
    "Manifest",
    "build_manifest",
    "find_citations",
    "is_settled",
    "load_manifest",
    "parse_register",
    "parse_revision",
    "render_report",
    "stale_markers",
]

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

SPEC_PATH = REPO_ROOT / "Nexus_AI_Detailed_Specification.md"
MANIFEST_PATH = BACKEND_ROOT / "tools" / "spec_issues.json"

#: The three states a register row may carry. **``DISPOSITIONED`` is R-62(4)'s vocabulary**
#: and is kept distinct from ``RESOLVED`` on purpose — it means *deferred with a checkable
#: revisit trigger*, which is a decision rather than a question ("a deferral with a trigger
#: is a decision; an open row is a question"). For every rule here the two behave alike, but
#: collapsing them in the manifest would throw away the distinction the sweep was written to
#: establish.
STATUSES = ("OPEN", "RESOLVED", "DISPOSITIONED")

#: Statuses that mean "no longer a question", and therefore may not back a `TBD` marker.
SETTLED = ("RESOLVED", "DISPOSITIONED")

#: Register rows carry their status as an explicit tag rather than in prose. Inferring it
#: from the row text was tried first and is **not reliable** — a row that resolves one
#: sub-part while another stays open reads identically to a closed one, and a row narrating
#: a *neighbouring* issue's history ("its cloud-drive half remains open") is misread as its
#: own. That mis-classified OI-17, OI-19 and OI-20 on the first pass.
_ROW_RE = re.compile(rf"^\| \*\*(OI-\d+)\*\* `\[({'|'.join(STATUSES)})\]`")

#: Any mention of an issue id, anywhere in a source file.
_CITATION_RE = re.compile(r"OI-\d+")

#: The provisional-value marker CLAUDE.md tells a session to grep for. Only the `OI-` form is
#: checkable here; `TBD(§8.4)` points at a prose bullet list with no stable ids.
_TBD_MARKER_RE = re.compile(r"TBD\((OI-\d+)\)")

_REVISION_RE = re.compile(r"^\| \*\*Status\*\* \| Rev ([0-9.]+) ")

#: Scanned for citations. `frontend/src` is included because T-405's generated client carries
#: docstring text into `schema.d.ts`, so a rotted docstring is visible there too.
CODE_ROOTS = (
    "backend/app",
    "backend/tests",
    "backend/workers",
    "backend/evals",
    "backend/tools",
    "frontend/src",
)

_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".css"})

#: The guard's own source and fixtures. Both quote marker syntax and issue ids verbatim —
#: they have to, to state and to test the rule — and a textual convention cannot tell a
#: quoted marker from a real one. So the two files that define the rule are outside it,
#: named explicitly rather than pattern-matched, so the exemption cannot quietly widen.
EXCLUDED = frozenset(
    {
        "backend/tools/spec_xref.py",
        "backend/tests/test_spec_xref.py",
    }
)


@dataclass(frozen=True, slots=True)
class Citation:
    """One mention of an issue id in a source file."""

    issue: str
    path: str
    line: int
    text: str
    #: True when the mention is a `TBD(OI-NN)` provisional marker rather than prose.
    is_marker: bool


@dataclass(frozen=True, slots=True)
class Manifest:
    """The committed projection of the register: ids and statuses, no spec prose."""

    revision: str
    issues: dict[str, str]

    def settled(self) -> list[str]:
        return sorted((k for k, v in self.issues.items() if v in SETTLED), key=lambda k: int(k[3:]))

    def open(self) -> list[str]:
        return sorted((k for k, v in self.issues.items() if v == "OPEN"), key=lambda k: int(k[3:]))


def parse_register(spec_text: str) -> dict[str, str]:
    """Read `OI-NN -> status` from the tagged register rows.

    Raises on a duplicate id: two rows for one issue is exactly the bookkeeping failure this
    tool exists to surface, and silently keeping the last would hide it.
    """
    found: dict[str, str] = {}
    for line in spec_text.splitlines():
        match = _ROW_RE.match(line)
        if match is None:
            continue
        issue, status = match.group(1), match.group(2)
        if issue in found:
            raise ValueError(f"{issue} appears twice in the register")
        found[issue] = status
    if not found:
        raise ValueError("no tagged register rows found — has the `[STATUS]` tag convention moved?")
    return found


def parse_revision(spec_text: str) -> str:
    """The spec revision from the header Status row, so a stale manifest is visible."""
    for line in spec_text.splitlines():
        match = _REVISION_RE.match(line)
        if match is not None:
            return match.group(1)
    raise ValueError("no `| **Status** | Rev …` header row found")


def is_settled(status: str) -> bool:
    return status in SETTLED


def find_citations(repo_root: pathlib.Path, roots: tuple[str, ...] = CODE_ROOTS) -> list[Citation]:
    """Every mention of an issue id under `roots`, ordered by path then line.

    A missing root is skipped rather than raised on: the backend suite must pass in a
    checkout that has no `frontend/`.
    """
    out: list[Citation] = []
    for root in roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in _SUFFIXES or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if rel in EXCLUDED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                markers = set(_TBD_MARKER_RE.findall(line))
                for issue in sorted(set(_CITATION_RE.findall(line))):
                    out.append(
                        Citation(
                            issue=issue,
                            path=rel,
                            line=number,
                            text=line.strip(),
                            is_marker=issue in markers,
                        )
                    )
    return out


def stale_markers(citations: list[Citation], manifest: Manifest) -> list[Citation]:
    """`TBD(OI-NN)` markers whose issue is no longer a question — the hard rule.

    A marker citing an id the manifest does not know at all is stale too: it is either a typo
    or a reference to a row that was renumbered, and neither should survive silently.
    """
    return [
        c for c in citations if c.is_marker and manifest.issues.get(c.issue, "RESOLVED") in SETTLED
    ]


def build_manifest(spec_text: str) -> Manifest:
    return Manifest(revision=parse_revision(spec_text), issues=parse_register(spec_text))


def load_manifest(path: pathlib.Path = MANIFEST_PATH) -> Manifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Manifest(revision=raw["spec_revision"], issues=dict(raw["issues"]))


def write_manifest(manifest: Manifest, path: pathlib.Path = MANIFEST_PATH) -> None:
    payload = {
        "_comment": (
            "Generated by `uv run python -m tools.spec_xref --write` (T-607). Ids and statuses "
            "only — the spec itself is not published. Do not hand-edit: `tests/test_spec_xref.py` "
            "fails when this stops matching the register."
        ),
        "spec_revision": manifest.revision,
        "issues": {
            key: manifest.issues[key] for key in sorted(manifest.issues, key=lambda k: int(k[3:]))
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def render_report(citations: list[Citation], manifest: Manifest) -> str:
    """The half a human reads before ruling on an issue."""
    lines = [
        f"spec Rev {manifest.revision} — {len(manifest.open())} open, "
        f"{len(manifest.settled())} settled"
    ]

    stale = stale_markers(citations, manifest)
    lines.append("")
    lines.append(f"HARD RULE — TBD markers citing a settled issue: {len(stale)}")
    for citation in stale:
        status = manifest.issues.get(citation.issue, "unknown to the manifest")
        lines.append(f"  {citation.path}:{citation.line}  {citation.issue} is {status}")
        lines.append(f"      {citation.text[:120]}")

    lines.append("")
    lines.append("REVIEW — open issues, and what the tree already says about them.")
    lines.append("  Read these before ruling: three times running, the answer was already here.")
    for issue in manifest.open():
        where = [c for c in citations if c.issue == issue]
        lines.append(f"  {issue}: {len(where)} site(s)")
        for citation in where:
            marker = " [TBD marker]" if citation.is_marker else ""
            lines.append(f"      {citation.path}:{citation.line}{marker}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--write", action="store_true", help="regenerate spec_issues.json from the spec"
    )
    args = parser.parse_args(argv)

    if args.write:
        if not SPEC_PATH.is_file():
            print(f"spec not found at {SPEC_PATH} — nothing to generate from", file=sys.stderr)
            return 2
        manifest = build_manifest(SPEC_PATH.read_text(encoding="utf-8"))
        write_manifest(manifest)
        print(
            f"wrote {MANIFEST_PATH.relative_to(REPO_ROOT).as_posix()} "
            f"({len(manifest.issues)} issues, spec Rev {manifest.revision})"
        )
        return 0

    manifest = load_manifest()
    citations = find_citations(REPO_ROOT)
    print(render_report(citations, manifest))
    return 1 if stale_markers(citations, manifest) else 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
