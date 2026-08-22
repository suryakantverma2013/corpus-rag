"""Every command the documentation teaches names something that exists (T-705).

**This guard runs nothing, and that is a decision rather than a shortcut.** The board line for
T-705 asked that "every fenced command block marked runnable actually runs". No runnable-marking
convention exists in this tree, and nothing here qualifies for one: every command block needs
Docker, a live stack, credentials, a browser, or real OpenAI spend. A guard gated on any of those
**skips**, and *a test that has only ever skipped is not a passing test* (Rev 0.12) is the rule four
other guard modules here are built on -- so an execution guard would be forbidden by this project's
own convention the moment it was honest about its preconditions. The two commands that are
side-effect-free (``tools.apidocs``, ``tools.httpdocs``) write files into the tree, and the second
is already covered in memory by `tests/test_http_docs.py`, which renders and compares a string
rather than invoking the CLI.

What ships instead is the checkable half, and it is where the rot actually lives: a renamed tool,
a deleted npm script, a compose profile that no longer exists. `tools/reembed.py` being renamed is
a realistic Tuesday; ``docker compose up`` ceasing to exist is not.

Commands are read from fenced blocks **and** inline spans -- ``python -m tools.httpdocs`` is only
ever written in backticks -- and backslash continuations are joined first, which is what lets a
``--profile`` be checked against the compose file named by its own command.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
from collections.abc import Iterator
from typing import Final

from tests.docs import BACKEND_ROOT, REPO_ROOT, ascii_only, documents

#: Where the documentation's commands are run from, in the order a reader would try them. The
#: repository root first, then the three directories whose READMEs give commands of their own.
_WORKING_DIRECTORIES: Final = (".", "backend", "frontend", "deployment")

_PYTHON_MODULE: Final = re.compile(r"python -m ([A-Za-z_][\w.]*)")
_NPM_SCRIPT: Final = re.compile(r"npm run ([\w:.-]+)")
_COMPOSE_FILE: Final = re.compile(r"-f\s+(\S*docker-compose[\w.-]*\.ya?ml)")
_PROFILE: Final = re.compile(r"--profile\s+([\w-]+)")
_PYTEST_TARGET: Final = re.compile(r"pytest\s+((?:tests|evals)[\w/.]*(?:::\w+)?)")
#: A `.sh` **with a directory component and no leading slash**. A bare filename is prose shorthand
#: for something the paragraph already named, and an absolute path is a binary inside somebody
#: else's image (`/opt/keycloak/bin/kc.sh`, `clamdcheck.sh`) -- neither is a claim about this
#: repository, and demanding an allowance for each would turn the guard into an argument about
#: other people's containers. A relative path is a claim, and that is what gets checked.
_SHELL_SCRIPT: Final = re.compile(r"(?<![\w.-])(/?(?:[\w.-]+/)+[\w.-]+\.sh)")
_DECLARED_PROFILES: Final = re.compile(r"profiles:\s*\[([^\]]*)\]")


def _commands() -> Iterator[tuple[str, str, int]]:
    """Every code fragment in the documentation, as `(text, document, line)`.

    Backslash continuations are joined so a multi-line `docker compose` invocation is one command.
    """
    for page in documents():
        for block in page.code:
            yield block.text.replace("\\\n", " "), page.path, block.line


def _sites(pattern: re.Pattern[str]) -> dict[str, set[str]]:
    """Every capture of `pattern` across the documentation, mapped to `document:line` sites."""
    found: dict[str, set[str]] = {}
    for text, path, line in _commands():
        for match in pattern.finditer(text):
            found.setdefault(match.group(1), set()).add(f"{path}:{line}")
    return found


def _resolve(path: str) -> pathlib.Path | None:
    """A path as a reader would follow it: from the repository root, or from a working directory.

    `backend/README.md` writes `-f ../deployment/docker-compose.yml` because its commands are run
    from `backend/`, which is correct and resolves nowhere from the root.
    """
    for base in _WORKING_DIRECTORIES:
        candidate = (REPO_ROOT / base / path).resolve()
        if candidate.is_file():
            return candidate
    return None


def _module_file(name: str) -> pathlib.Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except ImportError, ValueError:
        return None
    if spec is None or spec.origin in (None, "built-in"):
        return None
    return pathlib.Path(spec.origin)


def test_every_python_module_named_in_a_command_exists_and_is_runnable_as_a_module() -> None:
    """Importable is not enough: `python -m` on a module with no entry point does nothing at all."""
    broken: list[str] = []
    for name, sites in sorted(_sites(_PYTHON_MODULE).items()):
        where = ", ".join(sorted(sites))
        path = _module_file(name)
        if path is None:
            broken.append(f"  python -m {name}  ({where}) -- no such module")
            continue
        source = path.read_text(encoding="utf-8")
        runnable = '__name__ == "__main__"' in source or (path.parent / "__main__.py").exists()
        if not runnable:
            broken.append(
                f"  python -m {name}  ({where}) -- importable, but defines no "
                'if __name__ == "__main__" block, so `python -m` does nothing'
            )

    assert not broken, (
        f"{len(broken)} command(s) name a Python module that cannot be run:\n"
        + "\n".join(broken)
        + "\n\nA reader following the documentation gets ModuleNotFoundError, or silence. Rename "
        "the citation, or restore the module."
    )


def test_every_npm_script_named_in_a_command_is_declared_in_package_json() -> None:
    manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    declared = set(manifest.get("scripts", {}))

    broken = [
        f"  npm run {name}  ({', '.join(sorted(sites))})"
        for name, sites in sorted(_sites(_NPM_SCRIPT).items())
        if name not in declared
    ]
    assert not broken, (
        f"{len(broken)} command(s) name an npm script that is not declared:\n"
        + "\n".join(broken)
        + f"\n\nfrontend/package.json declares: {sorted(declared)}"
    )


def test_every_compose_file_and_profile_named_in_a_command_exists() -> None:
    """A profile is checked against the compose file its own command names, where there is one.

    `--profile` on a file that does not declare it is silent: compose starts the services that
    have no profile and none of the ones you asked for.
    """
    missing_files: list[str] = []
    for name, sites in sorted(_sites(_COMPOSE_FILE).items()):
        if _resolve(name) is None:
            missing_files.append(f"  -f {name}  ({', '.join(sorted(sites))})")

    def declared_in(paths: list[str]) -> set[str]:
        names: set[str] = set()
        for path in paths:
            file = _resolve(path)
            if file is None:
                continue
            for group in _DECLARED_PROFILES.findall(file.read_text(encoding="utf-8")):
                names |= {p.strip().strip("\"'") for p in group.split(",") if p.strip()}
        return names

    every_compose = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "deployment").glob("docker-compose*.yml")
    )

    missing_profiles: list[str] = []
    for text, path, line in _commands():
        named = _COMPOSE_FILE.findall(text)
        available = declared_in(named or every_compose)
        for profile in _PROFILE.findall(text):
            if profile not in available:
                missing_profiles.append(
                    f"  --profile {profile}  ({path}:{line})"
                    f" -- not declared in {named or 'any compose file'}"
                )

    assert not missing_files, (
        "documented commands name compose files that do not exist:\n" + "\n".join(missing_files)
    )
    assert not missing_profiles, (
        "documented commands name compose profiles that are not declared:\n"
        + "\n".join(sorted(set(missing_profiles)))
        + "\n\ncompose accepts an unknown --profile silently and starts nothing extra."
    )


def test_every_pytest_target_named_in_a_command_exists() -> None:
    """Paths, and the `::test_name` suffix where one is written -- resolved by parsing, not running.

    Collecting would run `conftest` and cost the whole suite's import time inside one guard; the
    question here is only whether the named target still exists (`tests/acceptance`'s rule).
    """
    broken: list[str] = []
    for target, sites in sorted(_sites(_PYTEST_TARGET).items()):
        where = ", ".join(sorted(sites))
        path_part, _, name = target.partition("::")
        path = BACKEND_ROOT / path_part
        if not path.exists():
            broken.append(f"  pytest {target}  ({where}) -- {path_part} does not exist")
            continue
        if not name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if name not in defined:
            broken.append(f"  pytest {target}  ({where}) -- {path_part} defines no {name}")

    assert not broken, f"{len(broken)} documented pytest target(s) no longer exist:\n" + "\n".join(
        broken
    )


def test_every_shell_script_path_named_in_the_documentation_exists() -> None:
    broken: list[str] = []
    for name, sites in sorted(_sites(_SHELL_SCRIPT).items()):
        if name.startswith("/"):
            continue  # inside a container image, not in this repository
        if not _resolve(name.removeprefix("./")):
            broken.append(f"  {name}  ({', '.join(sorted(sites))}) -- no such file")

    assert not broken, (
        f"{len(broken)} shell script path(s) named in the documentation do not exist:\n"
        + "\n".join(broken)
    )


def test_the_commands_the_documentation_teaches_are_not_an_empty_set() -> None:
    """Anti-vacuity: every assertion above is satisfied by finding nothing at all."""
    modules = _sites(_PYTHON_MODULE)
    scripts = _sites(_NPM_SCRIPT)
    compose = _sites(_COMPOSE_FILE)
    profiles = _sites(_PROFILE)
    pages = {
        site.split(":")[0]
        for sites in (modules, scripts, compose)
        for s in sites.values()
        for site in s
    }

    assert len(modules) >= 8, f"only {len(modules)} python -m commands found: {sorted(modules)}"
    assert len(scripts) >= 10, f"only {len(scripts)} npm run commands found: {sorted(scripts)}"
    assert len(compose) >= 2, f"only {len(compose)} compose files cited: {sorted(compose)}"
    assert len(profiles) >= 3, f"only {len(profiles)} compose profiles cited: {sorted(profiles)}"
    assert len(pages) >= 6, f"commands appear in only {len(pages)} document(s): {sorted(pages)}"
    assert all(ascii_only(name) == name for name in modules)
