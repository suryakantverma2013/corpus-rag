"""Render the backend source reference from the docstrings already in the tree (T-703).

    cd backend && uv run python -m tools.apidocs          # render to docs/reference/backend
    cd backend && uv run python -m tools.apidocs --serve  # live preview on localhost

**Why a generator rather than written pages.** Every backend module here carries a docstring, and
those docstrings are not summaries — they carry the rulings, the rejected alternatives and the
traps (`workers/delete.py` explains its purge-before-commit ordering; `app/services/health.py`
explains why worker readiness is a second endpoint). A hand-written page per module would be a
second copy of that, and the second copy is the one that goes stale. So the reference is rendered
from the source, and the half a generator *cannot* produce — what a package owns, what it must not
do, and where its rules are enforced — is written by hand in `docs/MODULE_MAP.md`.

**The output is gitignored on purpose.** A generated file committed to the tree is a stale file
waiting to happen: it changes on every docstring edit, nobody reviews the diff, and it silently
stops matching. Regenerate it, never edit it. (The one deliberate exception is `docs/HTTP_API.md`,
which is committed *with a drift test* — see `tools/httpdocs.py` for why that trade is different.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The packages that make up the deployable. `tests` is excluded: it is large, its docstrings speak
# to whoever is changing a test rather than to a reader of the system, and `tests/acceptance/` and
# `tests/security/` are already rendered for humans by their own tools.
#
# `!app.db.migrations` is a REQUIRED exclusion, not a preference: pdoc documents by importing, and
# Alembic's `env.py` reads `alembic.context`, a proxy that only exists while Alembic is driving.
# Importing it outside that context raises, and pdoc turns that into a hard failure for the whole
# run. The revision files are append-only history rather than API, so nothing is lost.
MODULES = ("app", "workers", "tools", "evals", "!app.db.migrations")

# Repo-root-relative, so the backend and frontend references sit side by side under docs/.
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "reference" / "backend"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.apidocs",
        description="Render the backend source reference with pdoc.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="output directory (gitignored)"
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run pdoc's live-reloading preview server instead of writing files",
    )
    parser.add_argument("--port", type=int, default=8080, help="port for --serve")
    args = parser.parse_args(argv)

    try:
        import pdoc
        import pdoc.web
    except ModuleNotFoundError:
        # pdoc is a dev-group dependency, so this is what an operator running from a production
        # image sees. Name the fix rather than letting an ImportError stand as the message.
        print(
            "pdoc is not installed. It lives in the dev dependency group:\n"
            "  cd backend && uv sync --group dev",
            file=sys.stderr,
        )
        return 2

    if args.serve:
        all_modules = pdoc.extract.walk_specs(MODULES)
        httpd = pdoc.web.DocServer(("localhost", args.port), all_modules)
        with httpd:
            url = f"http://localhost:{args.port}"
            print(f"apidocs: serving {len(all_modules)} modules at {url} (ctrl-c to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\napidocs: stopped")
        return 0

    args.output.mkdir(parents=True, exist_ok=True)
    pdoc.pdoc(*MODULES, output_directory=args.output)
    # Count what was produced rather than what was requested: a module that fails to import is
    # skipped by pdoc with a warning, and "rendered 136 modules" would paper over it.
    pages = sorted(args.output.rglob("*.html"))
    print(f"apidocs: rendered {len(pages)} pages -> {args.output}")
    print(f"apidocs: open {args.output / 'index.html'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
