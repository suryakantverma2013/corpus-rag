"""The environment-variable coverage guard (T-613).

Two templates ship in this repository and, until this module, **nothing tracked read either
one**. T-605 measured them against the settings model by hand, once, and the check was never
committed — so "the templates are complete" was a claim with no oracle, of exactly the kind
T-606 found four instances of in section 9.

It is a claim that rots by construction, and R-81(7) records both directions costing something
real. ``backend/.env.example`` had omitted ``ENVIRONMENT``, the one variable both production
boot-refusals key on, so an operator following it verbatim got ``development`` and **silently
disarmed the safety net** that stops a non-durable checkpointer and a clear-text refresh cookie
reaching production. In the same task a ``float | None`` was written as ``NAME=`` — a parse
error rather than "unset" — and the app refused to boot.

**Why a name diff and not "the app would fail to boot".** All 29 settings models set
``extra="ignore"``. A variable documented under a name no field reads is therefore accepted in
silence, and so is a field nobody documented: neither surfaces as an error anywhere, ever. The
name diff is the only instrument that sees them.

**The two templates are checked against different things, because they claim different things.**
``backend/.env.example`` documents the application's settings surface, so it is diffed both ways
against the model. ``deployment/.env.prod.example`` documents the *stack* — its own header says
the application's settings keep their defaults and that an override belongs in the compose
file's ``x-corpus-env`` block "since only variables referenced there are passed to a container"
— so it is diffed both ways against that file's ``${VAR}`` interpolations. Diffing it against
the model instead would need a ~148-name exclusion list, which is the same hand-maintained rot
this guard exists to remove.

**The oracle is the class, never a live ``Settings()``.** ``get_settings()`` is cached and
``tests/conftest.py`` loads the developer's ``.env`` into ``os.environ``, so an instance would
report *this box* rather than the shipped model — the rule ``tests/acceptance/__init__.py``
established for ``Default.check``, and the same reason.

**Nothing here skips.** All four inputs are tracked, unlike the gitignored spec that
``test_spec_xref.py`` guards against. A test that has only ever skipped is not a passing test
(Rev 0.12), and that cuts both ways: there is no condition under which this guard should
decline to run.

Today's figures, as narrative rather than as assertions — pinning them would make every new
setting touch a number, which is the rot again: 159 settable names, 158 of them active in
``backend/.env.example`` alongside 8 documented non-settings names, 1 deliberately commented
out; 28 interpolations matching 28 documented stack variables; 32 container-environment keys.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shutil
import types
from collections.abc import Mapping
from unittest import mock

import pytest
from pydantic_settings import BaseSettings

from app.config import Settings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND_ENV = REPO_ROOT / "backend" / ".env.example"
PROD_ENV = REPO_ROOT / "deployment" / ".env.prod.example"
PROD_COMPOSE = REPO_ROOT / "deployment" / "docker-compose.prod.yml"


# --- allowances -------------------------------------------------------------------------
#
# Each is a name -> reason mapping rather than a set. A set grows by one token in a diff and
# leaves the reviewer nothing to review; a mapping cannot grow without someone writing the
# sentence that justifies it, which is the discipline `ResidualGap` already imposes next door.
# The reason is printed in the failure message, so whoever trips the check learns why the
# neighbours are allowed.
#
# Their sizes are deliberately NOT pinned: constant and assertion would live in one module, so
# the pin is a tautology a single edit satisfies. The non-tautological form is the pair of
# "every allowance is still exercised" tests below — a stale entry fails rather than lingering
# to excuse the next drift.

#: Documented in `backend/.env.example`, read by something other than `Settings`.
NOT_SETTINGS: Mapping[str, str] = types.MappingProxyType(
    {
        "POSTGRES_HOST": "compose `postgres` profile only, per its own comment in the template",
        "POSTGRES_PORT": "compose `postgres` profile only",
        "POSTGRES_DB": "compose `postgres` profile only",
        "POSTGRES_USER": "compose `postgres` profile only",
        "POSTGRES_PASSWORD": "compose `postgres` profile only",
        "KEYCLOAK_ADMIN_USER": "master-realm console credential; no field reads it",
        "KEYCLOAK_ADMIN_PASSWORD": "master-realm console credential; read by tooling, not the app",
        "KEYCLOAK_LIVE_ADMIN_PASSWORD": "live-test gate, read from os.environ by tests/conftest.py",
    }
)

#: Documented but deliberately inactive. The one entry is R-81(7)'s second defect.
INTENTIONALLY_INACTIVE: Mapping[str, str] = types.MappingProxyType(
    {
        "SSE_STALL_AFTER_SECONDS": (
            "float | None: an empty value is a parse error, not 'unset', so the template "
            "documents it commented out and lets WORKER_JOB_TIMEOUT_SECONDS + 60 derive it "
            "(R-81(7))"
        ),
    }
)

#: Stack variables that begin with a real `env_prefix` but name no field.
COMPOSE_ONLY_UNDER_A_PREFIX: Mapping[str, str] = types.MappingProxyType(
    {
        "KEYCLOAK_DB_PASSWORD": "the Keycloak service's own Postgres role; never read by the app",
        "KEYCLOAK_PORT": "host port for the admin console, bound to loopback",
        "MINIO_ROOT_USER": "MinIO server credential; reaches the app as MINIO_ACCESS_KEY",
        "MINIO_ROOT_PASSWORD": "MinIO server credential; reaches the app as MINIO_SECRET_KEY",
    }
)

#: Container environment in the compose file that is not a Corpus setting.
NOT_A_CORPUS_SETTING: Mapping[str, str] = types.MappingProxyType(
    {
        "DEEPEVAL_CACHE_FOLDER": "vendor cache directory for the worker's judge (T-309)",
    }
)


# --- reading the settings model ---------------------------------------------------------


def _settings_env_names() -> dict[str, str]:
    """Every variable pydantic-settings would read, composed off the classes.

    ``model_fields`` is the whole exclusion rule: it already omits the seven ``@computed_field``
    properties, the plain ``@property`` (``Settings.stall_after``), the ``ClassVar``
    (``STALL_MARGIN_SECONDS``) and module constants such as ``EMBEDDING_MAX_INPUT_CHARS`` —
    which is worth naming because it reads exactly like an ``EMBEDDING_`` variable and is not
    one. Writing a filter for any of them would read as load-bearing while never firing.
    """
    names: dict[str, str] = {}
    for field, info in Settings.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseSettings):
            prefix = annotation.model_config.get("env_prefix", "")
            for sub in annotation.model_fields:
                names[f"{prefix}{sub.upper()}"] = f"{annotation.__name__}.{sub}"
        else:
            names[field.upper()] = f"Settings.{field}"
    return names


def _settings_groups() -> list[type[BaseSettings]]:
    return [
        info.annotation
        for info in Settings.model_fields.values()
        if isinstance(info.annotation, type) and issubclass(info.annotation, BaseSettings)
    ]


def _group_prefixes() -> frozenset[str]:
    return frozenset(group.model_config.get("env_prefix", "") for group in _settings_groups())


# --- reading the templates --------------------------------------------------------------

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclasses.dataclass(frozen=True, slots=True)
class Template:
    """One env template, split by whether a line actually declares anything.

    ``commented`` is never "documented" on its own — the production template's header carries
    four commented *example* assignments that would otherwise masquerade as declarations. It is
    only ever consulted through :data:`INTENTIONALLY_INACTIVE`.
    """

    path: pathlib.Path
    active: dict[str, str]
    commented: frozenset[str]
    unparsed: tuple[str, ...]


def _parse_template(path: pathlib.Path, *, strip_inline_comments: bool = False) -> Template:
    active: dict[str, str] = {}
    commented: set[str] = set()
    unparsed: list[str] = []
    # `splitlines()` rather than `.split("\n")`: it drops a trailing \r, so a CRLF checkout
    # parses identically (test_openapi_contract.py's warning, one file over).
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            match = _ASSIGNMENT.fullmatch(stripped.lstrip("#").strip())
            if match:
                commented.add(match.group(1))
            continue
        match = _ASSIGNMENT.fullmatch(stripped)
        if match is None:
            unparsed.append(stripped)
            continue
        value = match.group(2)
        if strip_inline_comments:
            value = re.sub(r"\s+#.*$", "", value)
        active[match.group(1)] = value
    return Template(path, active, frozenset(commented), tuple(unparsed))


# --- reading the compose file -----------------------------------------------------------
#
# Regex rather than PyYAML: PyYAML is installed transitively through `deepeval` and is not a
# declared dependency, so importing it here would be a package-policy defect of its own.

# The `(?<!\$)` guard is for compose's `$$` escape, which nothing uses today.
_INTERPOLATION = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")
_CONTAINER_ENV_KEY = re.compile(r"^  ([A-Z][A-Z0-9_]*): (.*)$")


def _compose_text() -> str:
    return PROD_COMPOSE.read_text(encoding="utf-8")


def _compose_interpolations() -> set[str]:
    return {match.group(1) for match in _INTERPOLATION.finditer(_compose_text())}


def _compose_container_env() -> dict[str, str]:
    """The ``x-corpus-env`` anchor block: what reaches the api and worker containers."""
    _, _, rest = _compose_text().partition("x-corpus-env: &corpus-env\n")
    assert rest, "the x-corpus-env anchor moved; this guard needs re-pointing"
    block = re.split(r"\n(?=\S)", rest, maxsplit=1)[0]
    return {
        match.group(1): _unquote(match.group(2).strip())
        for match in (_CONTAINER_ENV_KEY.fullmatch(line) for line in block.splitlines())
        if match is not None
    }


def _unquote(value: str) -> str:
    """Strip YAML quoting, which compose removes before the variable reaches the container.

    Six of these keys are quoted because YAML would otherwise type them (`"true"`, `"3310"`).
    Passing the quotes through makes `MINIO_SECURE` the literal string `"false"`, which pydantic
    rejects as a boolean — a defect in this reader that reads exactly like a defect in the file.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {chr(34), chr(39)}:
        return value[1:-1]
    return value


def _resolve(value: str, supplied: Mapping[str, str]) -> str:
    """Apply compose's ``${VAR}`` / ``${VAR:-default}`` substitution."""

    def one(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if supplied.get(name):
            return supplied[name]
        return default[2:] if default else ""

    return _INTERPOLATION.sub(one, value)


# --- constructing Settings from a template ----------------------------------------------


def _settings_from(
    env: Mapping[str, str], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Build ``Settings`` from ``env`` alone, with two isolations that are both load-bearing.

    * ``chdir`` — every group carries ``env_file=".env"`` and resolves it against the cwd *at
      construction time*. The groups are built by ``default_factory``, so
      ``Settings(_env_file=None)`` never reaches them; run from ``backend/`` they read the
      developer's real ``.env`` even with ``os.environ`` completely cleared. Measured.
    * ``clear=True`` — ``tests/conftest.py`` loads that same ``.env`` into ``os.environ`` and
      pins QUEUE_BACKEND / LLM_BACKEND / EMBEDDING_BACKEND / RATELIMIT_STORAGE_URI before any
      import. The environment outranks a dotenv file, so without the clear this would assert
      the test suite's configuration rather than the template's.

    ``get_settings()``'s cache is deliberately neither read nor cleared: clearing it inside the
    patched environment would repopulate it from the template and leak into every later test.
    """
    monkeypatch.chdir(tmp_path)
    with mock.patch.dict(os.environ, dict(env), clear=True):
        return Settings()


def _settings_from_template(
    template: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Perform the operator's documented ``cp .env.example .env`` and boot from the result.

    The copy is the point: it takes this module's own parser off the load-bearing path. The
    operator's file is read by **python-dotenv**, which disagrees with any hand-rolled split on
    quoting, ``export``, inline comments and multi-line values — so a check driven from a parsed
    map could pass while the real file fails to boot, which is the failure this exists to catch.
    """
    shutil.copyfile(template, tmp_path / ".env")
    return _settings_from({}, tmp_path, monkeypatch)


def _assert_the_template_was_actually_read(settings: Settings) -> None:
    """Anti-vacuity (section 8.65(5)).

    If the copy or the chdir silently did nothing, ``Settings()`` builds from defaults and every
    assertion downstream passes green. Both sentinels are values where the template and the
    ambient test environment provably disagree.
    """
    assert "CHANGEME" in settings.database.url, (
        "the template's DATABASE_URL did not reach Settings — the copy or the chdir is not "
        "working, and the checks below would pass against the shipped defaults"
    )
    assert (settings.embedding.backend, settings.llm.backend, settings.queue.backend) == (
        "openai",
        "openai",
        "arq",
    ), "conftest.py's fake/fake/none pins leaked in — os.environ was not cleared"


# --- the backend template against the settings model ------------------------------------


def test_every_settings_variable_is_documented_in_the_backend_template() -> None:
    """R-81(7) defect 1: a field with no line strands the operator who never sets it."""
    template = _parse_template(BACKEND_ENV)
    missing = [
        f"{name} ({origin})"
        for name, origin in sorted(_settings_env_names().items())
        if name not in template.active
        and not (name in INTENTIONALLY_INACTIVE and name in template.commented)
    ]
    assert not missing, "settings with no line in backend/.env.example:\n  " + "\n  ".join(missing)


def test_every_backend_template_variable_is_a_field_or_a_named_exception() -> None:
    """The other direction, and ``extra="ignore"`` is why it needs an instrument at all."""
    template = _parse_template(BACKEND_ENV)
    known = _settings_env_names()
    unknown = [
        name for name in sorted(template.active) if name not in known and name not in NOT_SETTINGS
    ]
    assert not unknown, (
        "backend/.env.example documents variables no field reads (a typo here is silent, "
        "because every settings model sets extra='ignore'):\n  " + "\n  ".join(unknown)
    )


def test_the_backend_template_constructs_settings_as_written(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-81(7) defect 2, which no name-level diff can see.

    ``SSE_STALL_AFTER_SECONDS=`` is a perfectly good line naming a perfectly real field, and it
    stops the app booting. Copying the file and letting it construct is the only check here that
    looks at values — and it exercises the per-group and cross-group ``_coherent`` validators
    against the template's own numbers at the same time.
    """
    settings = _settings_from_template(BACKEND_ENV, tmp_path, monkeypatch)
    _assert_the_template_was_actually_read(settings)
    # The template ships `development` deliberately: the two production refusals stay dormant,
    # which is what makes this a check on the file rather than on `Settings._coherent`.
    assert settings.environment == "development"


def test_the_composed_name_rule_still_holds_for_every_settings_field() -> None:
    """The guard's premise, asserted rather than assumed.

    Every check here composes ``env_prefix + FIELD.upper()``. One ``validation_alias``, one
    ``alias_generator`` or one ``env_nested_delimiter`` and that rule silently stops describing
    what pydantic-settings reads — the diffs would then compare the wrong names and stay green.
    """
    broken: list[str] = []
    for model in (Settings, *_settings_groups()):
        for key in ("env_nested_delimiter", "alias_generator"):
            if model.model_config.get(key):
                broken.append(f"{model.__name__} sets {key}, which moves the composed names")
        for field, info in model.model_fields.items():
            if info.alias or info.validation_alias:
                broken.append(f"{model.__name__}.{field} carries an alias")
    prefixes = [group.model_config.get("env_prefix", "") for group in _settings_groups()]
    if not all(prefixes):
        broken.append("a nested settings group carries no env_prefix")
    if len(set(prefixes)) != len(prefixes):
        broken.append("two nested settings groups share an env_prefix")
    if Settings.model_config.get("env_prefix", ""):
        broken.append("Settings itself now carries an env_prefix; the top-level names moved")
    factories = sum(info.default_factory is not None for info in Settings.model_fields.values())
    if factories != len(_settings_groups()):
        broken.append(
            f"{factories} default_factory fields but {len(_settings_groups())} groups found by "
            "annotation — the walk and the model disagree"
        )
    assert not broken, "the composed-name rule no longer holds:\n  " + "\n  ".join(broken)


def test_every_backend_allowance_is_still_exercised_and_carries_a_reason() -> None:
    """A dead allowance must be deleted, not left behind to excuse the next drift."""
    template = _parse_template(BACKEND_ENV)
    stale = [
        f"{name}: documented nowhere in {BACKEND_ENV.name}"
        for name in sorted(NOT_SETTINGS)
        if name not in template.active
    ]
    stale += [
        f"{name}: not commented out in {BACKEND_ENV.name}"
        for name in sorted(INTENTIONALLY_INACTIVE)
        if name not in template.commented
    ]
    stale += [
        f"{name}: allowed with no reason"
        for allowance in (NOT_SETTINGS, INTENTIONALLY_INACTIVE)
        for name, reason in allowance.items()
        if not reason.strip()
    ]
    assert not stale, "backend allowances that no longer describe the file:\n  " + "\n  ".join(
        stale
    )


def test_the_stall_threshold_is_documented_commented_out_and_not_as_an_empty_value() -> None:
    """Pins the template's own "LEAVE THIS COMMENTED OUT" instruction.

    Deliberately fragile: should a future ruling ship ``SSE_STALL_AFTER_SECONDS`` uncommented,
    this fails by design and the allowance moves with it. That is the intended cost of writing
    the reason down where the next reader will meet it.
    """
    template = _parse_template(BACKEND_ENV)
    assert "SSE_STALL_AFTER_SECONDS" in template.commented
    assert "SSE_STALL_AFTER_SECONDS" not in template.active


def test_neither_template_carries_a_line_that_declares_nothing() -> None:
    """``export FOO=1`` or a stray word parses as neither a comment nor an assignment."""
    for template in (
        _parse_template(BACKEND_ENV),
        _parse_template(PROD_ENV, strip_inline_comments=True),
    ):
        assert not template.unparsed, f"{template.path.name}: {template.unparsed}"


# --- the production template against the compose file -----------------------------------


def test_every_compose_interpolation_is_documented_in_the_prod_template() -> None:
    """An undocumented ``${VAR}`` is a stack that comes up misconfigured with no warning."""
    template = _parse_template(PROD_ENV, strip_inline_comments=True)
    missing = sorted(_compose_interpolations() - set(template.active))
    assert not missing, (
        "docker-compose.prod.yml interpolates variables .env.prod.example does not "
        "document:\n  " + "\n  ".join(missing)
    )


def test_every_prod_template_variable_is_interpolated_by_the_compose_file() -> None:
    """The template's own rule: only what the compose file references reaches a container."""
    template = _parse_template(PROD_ENV, strip_inline_comments=True)
    unused = sorted(set(template.active) - _compose_interpolations())
    assert not unused, (
        ".env.prod.example documents variables docker-compose.prod.yml never reads, so setting "
        "them does nothing:\n  " + "\n  ".join(unused)
    )


def test_a_prod_variable_under_a_settings_prefix_names_a_real_field() -> None:
    """The rename typo: ``KEYCLOAK_INTERNAL_URLS`` is accepted in silence by ``extra="ignore"``."""
    template = _parse_template(PROD_ENV, strip_inline_comments=True)
    known = _settings_env_names()
    prefixes = tuple(sorted(_group_prefixes()))
    broken = [
        name
        for name in sorted(template.active)
        if name.startswith(prefixes)
        and name not in known
        and name not in COMPOSE_ONLY_UNDER_A_PREFIX
    ]
    assert not broken, (
        "these look like settings and name no field — a typo, or an entry missing from "
        "COMPOSE_ONLY_UNDER_A_PREFIX:\n  " + "\n  ".join(broken)
    )


def test_every_prod_allowance_is_still_exercised_and_carries_a_reason() -> None:
    template = _parse_template(PROD_ENV, strip_inline_comments=True)
    stale = [
        f"{name}: no longer in {PROD_ENV.name}"
        for name in sorted(COMPOSE_ONLY_UNDER_A_PREFIX)
        if name not in template.active
    ]
    stale += [
        f"{name}: allowed with no reason"
        for name, reason in COMPOSE_ONLY_UNDER_A_PREFIX.items()
        if not reason.strip()
    ]
    assert not stale, "prod allowances that no longer describe the file:\n  " + "\n  ".join(stale)


# --- the compose file's container environment -------------------------------------------
#
# `x-corpus-env` is the surface that actually reaches the api and worker containers, and it was
# unguarded. A typo in one of its keys is swallowed by `extra="ignore"`, so the operator sees a
# healthy stack quietly running on a default.


def test_every_container_variable_in_the_compose_file_names_a_real_setting() -> None:
    container_env = _compose_container_env()
    assert container_env, "the x-corpus-env block parsed empty; this guard needs re-pointing"
    known = _settings_env_names()
    unknown = [
        name
        for name in sorted(container_env)
        if name not in known and name not in NOT_A_CORPUS_SETTING
    ]
    assert not unknown, (
        "docker-compose.prod.yml passes variables no field reads, so they configure "
        "nothing:\n  " + "\n  ".join(unknown)
    )


def test_the_production_stack_environment_constructs_settings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped stack boots, and still holds the two guarantees R-81(7) found disarmed.

    Resolves the compose file's container environment against the production template exactly as
    compose would, then constructs ``Settings`` from it. Nothing else in the repository checks
    that R-42(9) (a durable checkpointer in production) and R-72(1) (a Secure refresh cookie)
    are satisfied by what ships, as opposed to being enforceable in principle.
    """
    supplied = _parse_template(PROD_ENV, strip_inline_comments=True).active
    resolved = {
        name: _resolve(value, supplied)
        for name, value in _compose_container_env().items()
        if name not in NOT_A_CORPUS_SETTING
    }
    settings = _settings_from(resolved, tmp_path, monkeypatch)
    assert settings.environment == "production"
    assert settings.checkpointer.backend == "postgres"
    assert settings.session.refresh_cookie_secure is True


# --- the templates are published --------------------------------------------------------


def test_both_templates_are_committed_rather_than_gitignored() -> None:
    """R-81(7) defect 3.

    ``.env.prod.example`` was written, never committed, and absent from the published
    repository — matched by ``.env.*``, whose ``!.env.example`` negation is per filename.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for template in (BACKEND_ENV, PROD_ENV):
        assert f"!{template.name}" in gitignore, (
            f"{template.name} matches `.env.*` and has no negation line, so it is gitignored "
            "and will be missing from the published repository"
        )
