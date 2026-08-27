"""The acceptance manifest: §9's literal table and §5's NFR checklist, mapped to their evidence.

**The direction is the point.** The spec's §6 traceability matrix maps *source → requirement*.
Nothing in this repository mapped *requirement → evidence*, so "§9 is satisfied" was a claim no
file could check: every literal was pinned by whichever task shipped it, and a row nobody happened
to cover looked identical to a row everybody had.

Two things follow, and they are deliberately different in kind (``tools/spec_xref.py``'s split):

* a **hard rule** — every §9 row carries evidence, every pointer resolves, every NFR carries a
  disposition. That is objective, so it is ``test_completeness.py`` rather than advice.
* a **report** — ``tools/acceptance.py`` renders the same manifest for a human. Nothing there can
  be asserted; the point is that whoever signs the build off reads what covers what.

**Division of labour, so the next reader does not mistake it: the manifest's job is coverage, the
evidence's job is assertion.** A :class:`Source` or :class:`Fidelity` pointer does not re-assert a
frontend literal — the frontend's own suite does that, and re-asserting it here would be a second
copy to keep in step. What the pointer proves is that the assertion *exists*, and it fails the
moment the assertion is deleted or the literal is reworded out from under it.

Three pointer kinds do carry a **second oracle** (§8.65(5)), because they read the shipped value
back rather than a copy of it: :class:`Default` reads the field default off the settings model,
:class:`Constant` imports the module attribute, and :class:`Vocabulary` expands the live enum or
``Literal``. Those three fail on a changed literal even if every behavioural test still passes —
which is exactly the class of drift T-606 found four instances of.

**Why the row labels are committed rather than parsed at test time.** The spec is gitignored
(CLAUDE.md housekeeping — it is not ours to publish), so a check that parsed §9 directly would
``skip`` in the public repo, and *a test that has only ever skipped is not a passing test*
(Rev 0.12's lesson, R-57(2)'s precedent). The manifest therefore carries row labels and the
product's own strings only — no spec prose — and the drift check that re-reads §9 is spec-gated.
"""

from __future__ import annotations

import ast
import enum
import importlib
import pathlib
import typing
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BACKEND_ROOT",
    "Constant",
    "Default",
    "Disposition",
    "Evidence",
    "Fidelity",
    "NFR_DISPOSITIONS",
    "NfrRow",
    "PyTest",
    "RESIDUAL_GAPS",
    "REPO_ROOT",
    "SPEC_9_ROWS",
    "ResidualGap",
    "Source",
    "Vocabulary",
]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"

_FIDELITY_CHECKS = ("fidelity/run.mjs",) + tuple(
    f"fidelity/checks/{name}.mjs" for name in ("global", "layout", "copy", "surfaces")
)


# --- evidence kinds ---------------------------------------------------------------------
#
# Each kind answers `check()` with None when it resolves and a one-line reason when it does not.
# The reason is what the failing test prints, so it names the file and what was expected.


@dataclass(frozen=True, slots=True)
class PyTest:
    """A backend test whose failure would follow from the row's contract moving.

    Resolved by parsing the module rather than by collecting it: collection would run
    ``conftest`` and cost the whole suite's import time inside one guard, and the question here
    is only whether the named test still exists.
    """

    nodeid: str

    @property
    def label(self) -> str:
        return f"pytest  {self.nodeid}"

    def check(self) -> str | None:
        path_part, _, name = self.nodeid.partition("::")
        if not name:
            return f"{self.nodeid}: not a nodeid (expected 'path::test_name')"
        path = BACKEND_ROOT / path_part
        if not path.is_file():
            return f"{self.nodeid}: {path_part} does not exist"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if name not in defined:
            return f"{self.nodeid}: {path_part} defines no {name}"
        return None


@dataclass(frozen=True, slots=True)
class Default:
    """A shipped settings default, read off the model rather than off a live ``Settings``.

    ``get_settings()`` reads the environment and ``tests/conftest.py`` loads ``.env`` into it, so
    an instance would report *this box's* configuration. §9 fixes what the product ships, so the
    oracle is ``model_fields[...].default``.
    """

    target: str  # "app.config:UploadSettings"
    field: str
    expected: object

    @property
    def label(self) -> str:
        return f"default {self.target}.{self.field} == {self.expected!r}"

    def check(self) -> str | None:
        group = _import(self.target)
        if isinstance(group, str):
            return group
        fields = getattr(group, "model_fields", None)
        if fields is None or self.field not in fields:
            return f"{self.target} has no field {self.field}"
        actual = fields[self.field].default
        if actual != self.expected:
            return f"{self.target}.{self.field} defaults to {actual!r}, §9 says {self.expected!r}"
        return None


@dataclass(frozen=True, slots=True)
class Constant:
    """A module attribute §9 fixes verbatim — normative copy, an error code, a host."""

    target: str  # "app.rag.errors:ACCESS_DENIED"
    expected: object

    @property
    def label(self) -> str:
        return f"const   {self.target} == {self.expected!r}"

    def check(self) -> str | None:
        actual = _import(self.target)
        if isinstance(actual, str) and actual.startswith("cannot import "):
            return actual
        if actual != self.expected:
            return f"{self.target} is {actual!r}, §9 says {self.expected!r}"
        return None


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """A closed set §9 lists in full — a `StrEnum`'s values or a `Literal`'s members, in order."""

    target: str  # "app.db.enums:DocumentStatus"
    expected: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"vocab   {self.target} ({len(self.expected)} members)"

    def check(self) -> str | None:
        obj = _import(self.target)
        if isinstance(obj, str) and obj.startswith("cannot import "):
            return obj
        actual = _members(obj)
        if actual is None:
            return f"{self.target} is neither an enum nor a Literal alias"
        if actual != self.expected:
            return f"{self.target} is {actual}, §9 says {self.expected}"
        return None


@dataclass(frozen=True, slots=True)
class Source:
    """A literal in a source file — the cross-language pointer.

    ``frontend/src/tokens.test.ts`` already reads ``backend/app/tokens.py`` off disk for the same
    reason; this is that shape in the other direction.
    """

    path: str  # repo-relative
    needle: str

    @property
    def label(self) -> str:
        return f"source  {self.path} <- {self.needle!r}"

    def check(self) -> str | None:
        path = REPO_ROOT / self.path
        if not path.is_file():
            return f"{self.path} does not exist"
        if self.needle not in path.read_text(encoding="utf-8"):
            return f"{self.path} no longer contains {self.needle!r}"
        return None


@dataclass(frozen=True, slots=True)
class Fidelity:
    """A surface of the headed fidelity harness, named by its `r.context(...)` string.

    The harness is not part of ``npm test`` — it needs a browser, the dev server and a real
    login — so what is checkable here is that the surface still exists. That it *passes* is the
    live run, recorded in ``docs/ACCEPTANCE.md``.
    """

    surface: str

    @property
    def label(self) -> str:
        return f"fidelity {self.surface!r}"

    def check(self) -> str | None:
        needle = f"'{self.surface}'"
        for relative in _FIDELITY_CHECKS:
            path = REPO_ROOT / "frontend" / relative
            if path.is_file() and needle in path.read_text(encoding="utf-8"):
                return None
        return f"no fidelity check declares the surface {self.surface!r}"


type Evidence = PyTest | Default | Constant | Vocabulary | Source | Fidelity


def _import(target: str) -> object:
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - a broken import fails every other test too
        return f"cannot import {module_name}: {exc}"
    if not hasattr(module, attribute):
        return f"cannot import {target}: {module_name} has no {attribute}"
    return getattr(module, attribute)


def _members(obj: object) -> tuple[str, ...] | None:
    """The ordered members of a `StrEnum` class or of a `Literal` type alias."""
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        return tuple(str(member.value) for member in obj)
    inner = getattr(obj, "__value__", obj)  # `type X = Literal[...]` holds its RHS here
    args = typing.get_args(inner)
    return tuple(str(arg) for arg in args) if args else None


# --- §9: acceptance-critical literal values ----------------------------------------------
#
# Keys are §9's first column verbatim and in order; the drift guard compares them as a list.

SPEC_9_ROWS: dict[str, tuple[Evidence, ...]] = {
    "Brand name (default) / props": (
        Source("frontend/src/App.tsx", '"Corpus"'),
        Source("frontend/src/App.test.tsx", "FR-SYS-04 defaults (§9)"),
        Fidelity("§9 palette + tokens"),
    ),
    "Theme persistence key (R-58(1))": (
        Source("frontend/src/theme/theme.ts", "corpus.theme"),
        Source("frontend/src/theme/theme.test.ts", "corpus.theme"),
    ),
    "Accent-soft alpha (R-58(2))": (
        Source("frontend/src/styles/tokens.css", "--accent-soft-alpha: 14%"),
        Source("frontend/src/styles/tokens.css", "--accent-soft-alpha: 10%"),
        Fidelity("§9 palette + tokens"),
    ),
    "Scrollbar (NFR-USE-05, R-58(3)/R-60)": (
        Source("frontend/src/styles/tokens.css", "--scrollbar-thumb"),
        Fidelity("§9 scrollbar width"),
    ),
    "Focus ring (NFR-A11Y-02, R-59)": (
        Source("frontend/src/styles/tokens.css", "--focus-ring-width: 2px"),
        Source("frontend/src/styles/tokens.css", "--focus-ring-offset: 2px"),
        Fidelity("§9 palette + tokens"),
    ),
    "Reduced motion (NFR-A11Y-01, R-59)": (
        Source("frontend/src/styles/tokens.test.ts", "prefers-reduced-motion"),
        Fidelity("§9 reduced motion"),
    ),
    "Contrast conformance (NFR-A11Y-06)": (Source("frontend/ACCESSIBILITY.md", "NFR-A11Y-06"),),
    "Browser matrix (NFR-CMP-01, R-61)": (
        Source("frontend/vite.config.ts", "chrome111"),
        Source(
            "frontend/src/test/build-target.test.ts",
            "['chrome111', 'edge111', 'firefox114', 'safari16.4', 'ios16.4']",
        ),
    ),
    "Upload formats / max size / quota": (
        Default("app.config:UploadSettings", "max_file_bytes", 300 * 1024 * 1024),
        Default("app.config:UploadSettings", "user_quota_bytes", 10 * 1024 * 1024 * 1024),
        PyTest("tests/test_upload.py::test_all_four_formats_accepted"),
        PyTest("tests/test_upload.py::test_oversize_rejected_before_any_storage_write"),
        PyTest("tests/test_upload.py::test_quota_exceeded_rejected"),
    ),
    "Cloud import (FR-KBM-10, T-214)": (
        Default("app.config:CloudDriveSettings", "api_base", "https://www.googleapis.com"),
        PyTest("tests/test_cloud_import.py::test_a_file_id_that_is_not_a_file_id_is_refused"),
        Source("frontend/src/cloud/cloud.ts", "linked"),
    ),
    "Cloud realm grants (T-214, §8.53)": (
        PyTest(
            "tests/test_account_linking.py::test_the_service_account_can_resolve_the_broker_client"
        ),
        PyTest("tests/test_cloud_import.py::test_the_link_hash_is_keycloaks_formula"),
    ),
    "Drop-zone caption": (
        Source("frontend/src/kb/DropZone.tsx", "PDF · DOCX · CSV · MD — max 300 MB"),
        Fidelity("§9 knowledge-base copy"),
    ),
    "Upload-scope control *(Rev 0.38, R-71(3))*": (
        Source("frontend/src/kb/DropZone.tsx", "This chat"),
        Fidelity("§9 knowledge-base copy"),
    ),
    "FR-KBM-04 meta line *(Rev 0.38, R-71(2)/(4))*": (
        Source("frontend/src/kb/documents.ts", "still answering"),
        Source("frontend/src/kb/documents.test.ts", "still answering"),
    ),
    "Processing-lock `409` *(Rev 0.38, R-71(1))*": (
        Source("backend/app/api/errors.py", 'error_code: Literal["PROCESSING_LOCKED"]'),
        PyTest(
            "tests/test_documents_api.py"
            "::test_the_four_mutating_verbs_are_gated_while_a_response_generates"
        ),
    ),
    "Telemetry retention (NFR-OBS-04, R-79(3))": (
        Default("app.config:TelemetrySettings", "retention_days", 90),
        Default("app.config:TelemetrySettings", "retention_batch", 5_000),
        PyTest("tests/test_turn_telemetry.py::test_retention_defaults_are_the_ruled_ones"),
        PyTest(
            "tests/test_turn_telemetry.py"
            "::test_a_zero_horizon_keeps_everything_rather_than_deleting_it"
        ),
    ),
    "Telemetry tracing (NFR-OBS-05, R-79(4))": (
        Default("app.config:TelemetrySettings", "tracing_enabled", False),
        PyTest("tests/test_turn_telemetry.py::test_tracing_is_off_by_default"),
        PyTest("tests/test_turn_telemetry.py::test_the_span_exports_no_payload_text"),
    ),
    "Request correlation (NFR-OBS-01, R-79(2))": (
        PyTest("tests/test_turn_telemetry.py::test_every_response_carries_a_correlation_id"),
        PyTest("tests/test_turn_telemetry.py::test_a_hostile_correlation_id_is_replaced_not_bound"),
    ),
    "Context window limit / label format": (
        Default("app.config:ContextSettings", "window_tokens", 10_400),
        PyTest("tests/test_budget.py::test_usage_carries_the_configured_reserve"),
        Source("frontend/src/stats/stats.test.ts", "11.0K / 10.4K"),
        Fidelity("§9 stats copy"),
    ),
    "Composer placeholder": (
        Source("frontend/src/composer/Composer.tsx", "Ask a question — @ to cite a document"),
        Fidelity("§9 chat copy"),
    ),
    "Composer footer": (
        # The needle carries the Shift+Enter clause deliberately (R-91(5)): a prefix match on
        # "Responses grounded in" would survive the literal changing, and this row exists to
        # notice exactly that.
        Source(
            "frontend/src/composer/Composer.tsx",
            "Enter to send, Shift+Enter for a new line",
        ),
        Fidelity("§9 chat copy"),
    ),
    "Mention-menu header": (
        Source("frontend/src/composer/MentionMenu.tsx", "REFERENCE A DOCUMENT"),
        Fidelity("§9 mention menu"),
    ),
    "KB modal subtitle": (
        Source(
            "frontend/src/kb/KnowledgeBaseModal.tsx",
            "Global documents are searched in every chat; attachments only in this one.",
        ),
        Fidelity("§9 knowledge-base copy"),
    ),
    "Empty-state title": (
        Source("frontend/src/chat/MessageList.tsx", "Ask your knowledge base"),
        Fidelity("§9 empty states"),
    ),
    "DeepEval labels": (
        Source("frontend/src/stats/StatsPanel.tsx", "Ctx Precision"),
        Source("frontend/src/stats/StatsPanel.tsx", "Ctx Recall"),
        Fidelity("§9 stats copy"),
    ),
    "DeepEval score tooltip *(Rev 0.37, R-70)*": (
        Source(
            "frontend/src/chat/messages.ts",
            "DeepEval metric — indicative judge score, not an exact measurement",
        ),
        Fidelity("§9 stats copy"),
    ),
    "Eval status colors": (
        Source("frontend/src/styles/tokens.css", "--eval-good: #4ec3a6"),
        Source("frontend/src/styles/tokens.css", "--eval-warn: #e8a34c"),
        Source("frontend/src/styles/tokens.css", "--eval-bad: #e86a8a"),
        Fidelity("§9 eval hues applied"),
    ),
    "Model card": (
        Source("frontend/src/stats/StatsPanel.tsx", "Context synthesis · grounding on"),
        PyTest("tests/test_config_api.py::test_it_reports_the_configured_chat_model"),
        Fidelity("§9 stats copy"),
    ),
    "Hover-card footer": (
        Source("frontend/src/chat/messages.ts", "Source passage"),
        Fidelity("§9 citation hover card"),
    ),
    "Access-denied / generic failure": (
        Constant("app.rag.errors:ACCESS_DENIED", "Error: Access Denied"),
        Constant("app.rag.errors:SYSTEM_FAILURE", "System Failure: Please try again."),
    ),
    "Injection-blocked turn *(Rev 0.11, R-44(5))*": (
        Source("backend/app/rag/errors.py", "BLOCKED_INJECTION = ("),
        PyTest("tests/test_graph.py::test_a_blocked_prompt_never_reaches_retrieval"),
    ),
    "Eval empty state / sources empty state": (
        Source("frontend/src/stats/StatsPanel.tsx", "Scores appear once a response is evaluated."),
        Source(
            "frontend/src/stats/StatsPanel.tsx",
            "None yet — answers will list their sources here.",
        ),
        Fidelity("§9 empty states"),
    ),
    "Layout widths": (
        Source("frontend/src/styles/tokens.css", "--sidebar-w: 264px"),
        Source("frontend/src/styles/tokens.css", "--stats-w: 272px"),
        Source("frontend/src/styles/tokens.css", "--app-min-h: 640px"),
        Fidelity("§9 layout widths"),
    ),
    "Fonts": (
        Source("frontend/src/styles/tokens.css", "Instrument Sans"),
        Source("frontend/src/styles/tokens.css", "JetBrains Mono"),
        Fidelity("§9 fonts"),
    ),
    "Document lifecycle states (Rev 0.5)": (
        Vocabulary(
            "app.db.enums:DocumentStatus",
            (
                "UPLOADED",
                "QUEUED",
                "PARSING",
                "CHUNKING",
                "EMBEDDING",
                "INDEXING",
                "ACTIVE",
                "FAILED",
                "DELETE_PENDING",
                "DELETING",
                "DELETED",
            ),
        ),
    ),
    "Async upload response / code (Rev 0.5)": (
        PyTest("tests/test_upload.py::test_upload_pdf_persists_rows_and_object"),
        PyTest("tests/test_upload.py::test_enqueue_failure_still_returns_202"),
    ),
    "Upload route / form fields (Rev 0.7, R-33)": (
        PyTest("tests/test_upload.py::test_chat_scope_requires_conversation_id"),
        PyTest("tests/test_upload.py::test_chat_scope_creates_and_reuses_conversation_kb"),
    ),
    "Duplicate-upload response (Rev 0.7, R-33)": (
        PyTest("tests/test_upload.py::test_duplicate_returns_200_and_does_not_reingest"),
        PyTest("tests/test_upload.py::test_same_bytes_in_different_kb_are_not_duplicates"),
    ),
    "Upload rejection codes (Rev 0.7, R-33)": (
        PyTest("tests/test_upload.py::test_oversize_rejected_before_any_storage_write"),
        PyTest("tests/test_upload.py::test_extension_spoofing_rejected"),
        PyTest("tests/test_upload.py::test_quota_exceeded_rejected"),
        PyTest("tests/test_upload.py::test_empty_file_rejected"),
        PyTest("tests/test_upload.py::test_chat_scope_foreign_conversation_is_404"),
    ),
    "Deletion route / response (Rev 0.7.7, R-39)": (
        PyTest("tests/test_deletion.py::test_delete_returns_the_pending_state_and_queues_one_job"),
        PyTest("tests/test_deletion.py::test_deleting_an_already_deleted_document_is_200"),
        PyTest("tests/test_deletion.py::test_a_foreign_document_is_404_not_403"),
    ),
    "Retry route / response (Rev 0.7.7, R-39)": (
        PyTest("tests/test_deletion.py::test_retry_of_a_failed_document_queues_a_fresh_job"),
        PyTest("tests/test_deletion.py::test_retry_is_409_for_anything_but_failed"),
        PyTest("tests/test_deletion.py::test_retry_of_a_foreign_document_is_404"),
    ),
    "Job-status route (Rev 0.7.7, R-39)": (
        PyTest("tests/test_jobs_api.py::test_the_owner_sees_the_full_retry_state"),
        PyTest("tests/test_jobs_api.py::test_a_foreign_job_is_404"),
    ),
    "Replace route / response (Rev 0.7.8, R-40)": (
        PyTest("tests/test_replace.py::test_replace_queues_a_new_version"),
        PyTest(
            "tests/test_replace.py::test_replacing_with_identical_bytes_is_200_and_queues_nothing"
        ),
        PyTest(
            "tests/test_replace.py::test_replace_is_409_for_a_document_that_is_not_active_or_failed"
        ),
        PyTest(
            "tests/test_replace.py"
            "::test_replacing_with_bytes_that_belong_to_another_live_document_is_409"
        ),
        PyTest("tests/test_replace.py::test_replace_of_a_foreign_document_is_404"),
    ),
    "Documents list route (Rev 0.7.8, R-40)": (
        PyTest("tests/test_documents_api.py::test_list_paginates_by_limit_and_offset"),
        PyTest(
            "tests/test_documents_api.py"
            "::test_list_is_ordered_newest_first_with_a_deterministic_tiebreak"
        ),
        PyTest(
            "tests/test_documents_api.py::test_list_excludes_deleted_documents_but_shows_deleting_ones"
        ),
        PyTest("tests/test_documents_api.py::test_list_rejects_an_out_of_range_page"),
    ),
    "Document get route (Rev 0.7.8, R-40)": (
        PyTest("tests/test_documents_api.py::test_get_exposes_no_storage_uri_and_no_chunk_id"),
        PyTest(
            "tests/test_documents_api.py::test_get_returns_a_deleted_document_with_its_terminal_state"
        ),
        PyTest("tests/test_documents_api.py::test_get_of_a_foreign_document_is_404_not_403"),
    ),
    # FR-ING-07. The five ParserSettings defaults and the three OcrSettings ones are the
    # policy R-88(11) bounds the feature with; the two PyTests are the properties those
    # numbers exist to buy - the ceiling stops recognition WITHOUT failing the document, and
    # the shipped defaults provably fit inside the shipped job timeout.
    "OCR (Rev 0.55, FR-ING-07, R-88)": (
        Default("app.config:ParserSettings", "ocr_enabled", False),
        Default("app.config:ParserSettings", "ocr_dpi", 300),
        Default("app.config:ParserSettings", "ocr_min_confidence", 60.0),
        Default("app.config:ParserSettings", "ocr_max_pages", 200),
        Default("app.config:ParserSettings", "ocr_budget_seconds", 600.0),
        Default("app.config:OcrSettings", "port", 8884),
        Default("app.config:OcrSettings", "timeout_seconds", 60.0),
        Default("app.config:OcrSettings", "languages", "eng"),
        PyTest("tests/test_ocr.py::test_the_feature_ships_off"),
        PyTest(
            "tests/test_recognition.py::test_the_shipped_defaults_fit_inside_the_shipped_job_timeout"
        ),
        PyTest(
            "tests/test_recognition.py"
            "::test_the_page_ceiling_stops_recognition_without_failing_the_document"
        ),
    ),
    # FR-ING-08, now over two mechanisms (R-89). The absent flag is the claim worth pinning and
    # it cannot be pinned as a Default - so the three floors stand for the policy, and the third
    # PyTest carries the recorded limitation (an unruled table is not detected), which is the
    # half of this row a reader is most likely to discover by surprise. The last two pointers
    # are the *declared* half: a DOCX and a Markdown table state their own header, so those are
    # the assertions that fail if either producer stops declaring one.
    "Tabular structure (Rev 0.56, FR-ING-08, R-88(5)/(6), R-89)": (
        Default("app.config:ParserSettings", "table_min_rows", 2),
        Default("app.config:ParserSettings", "table_min_columns", 2),
        Default("app.config:ParserSettings", "table_max_per_page", 10),
        PyTest("tests/test_tables.py::test_a_table_is_one_block_on_its_pages_own_locator"),
        PyTest(
            "tests/test_tables.py"
            "::test_a_table_split_by_the_block_ceiling_repeats_its_header_on_every_part"
        ),
        PyTest(
            "tests/test_tables.py::test_an_unruled_table_is_not_detected_and_is_the_recorded_limitation"
        ),
        PyTest("tests/test_tables.py::test_a_docx_table_declares_its_first_row_as_its_header"),
        PyTest("tests/test_tables.py::test_a_markdown_table_declares_its_thead_row_as_its_header"),
    ),
    # R-88(7), and the row T-220 asked for. Two of these three are second oracles (R-85(1)):
    # `Constant` and `Vocabulary` read the shipped value back, so they fail on a changed
    # literal even when every behavioural test still passes. That matters more here than
    # almost anywhere else - `PREPROCESSING_VERSION` is an `embedding_fingerprint` input, so
    # moving it re-embeds the corpus and leaving it stationary when the text changes is
    # worse: chunks keep vectors built from text that no longer exists.
    # The `Vocabulary` pointer is deliberately unchanged by R-89: T-223 added two producers of
    # `table`, and none of the three members moved. That is exactly R-88(7)'s distinction - the
    # marker is not a fingerprint input, so gaining a producer re-embeds nothing, while the text
    # those producers emit *did* change, which is why the `Constant` beside it had to move.
    "Preprocessing version (Rev 0.56, R-88(7), R-89)": (
        Constant("app.ingestion.parsers.base:PREPROCESSING_VERSION", "3"),
        Vocabulary("app.ingestion.parsers.base:Extraction", ("text", "ocr", "table")),
        PyTest(
            "tests/test_recognition.py::test_re_ingesting_a_recognised_document_reuses_every_vector"
        ),
    ),
    # FR-ING-09. The `Default`s are the policy R-94(7) bounds the feature with; the PyTests are
    # the three properties those numbers exist to buy, and each is a different failure direction.
    # `test_extraction_is_off_by_default_and_the_detector_does_not_read_the_flag` is the row's
    # most load-bearing pointer: it pins BOTH halves of R-94(7), that the feature ships off and
    # that the flag gates the extraction *pass* rather than the detector, which is what lets the
    # detector be tested without arming a container. The byte cap's test carries the drop-never-
    # truncate rule, and the clock's carries fail-open - a stopped pass keeps what it found.
    "Figure extraction (Rev 0.61, FR-ING-09, R-94)": (
        Default("app.config:ParserSettings", "figures_enabled", False),
        Default("app.config:ParserSettings", "figure_dpi", 150),
        Default("app.config:ParserSettings", "figure_min_width_points", 60.0),
        Default("app.config:ParserSettings", "figure_min_height_points", 60.0),
        Default("app.config:ParserSettings", "figure_min_area_fraction", 0.01),
        Default("app.config:ParserSettings", "figure_max_per_page", 10),
        Default("app.config:ParserSettings", "figure_merge_padding_points", 12.0),
        Default("app.config:ParserSettings", "figure_caption_max_distance_points", 40.0),
        Default("app.config:ParserSettings", "figure_max_per_document", 200),
        Default("app.config:ParserSettings", "figure_budget_seconds", 300.0),
        Default("app.config:ParserSettings", "figure_max_bytes", 8 * 1024 * 1024),
        PyTest(
            "tests/test_figures.py"
            "::test_extraction_is_off_by_default_and_the_detector_does_not_read_the_flag"
        ),
        PyTest("tests/test_figure_extraction.py::test_a_figure_over_the_byte_cap_is_dropped_not_truncated"),
        PyTest(
            "tests/test_figure_extraction.py::test_an_exhausted_clock_stops_the_pass_without_failing_it"
        ),
        PyTest("tests/test_figure_extraction.py::test_an_extracted_figure_carries_no_text_of_the_document"),
    ),
    # FR-CIT-07 and NFR-SEC-10 share a row because they are one surface from two sides: what is
    # rendered, and who may fetch it. The administrator pointer is the one to protect - it is the
    # single most likely thing for a later reader to "harmonise" with the four sibling
    # /documents routes, all of which DO widen under FR-USR-04. The `figures`-key pointer pins
    # absent-not-null, which a client distinguishes and a schema change would silently flip.
    "Figure display and serving (Rev 0.61, FR-CIT-07, NFR-SEC-10, R-94)": (
        PyTest("tests/test_figure_route.py::test_the_owner_is_served_the_raster_inline"),
        PyTest("tests/test_figure_route.py::test_an_administrator_gets_404_on_another_users_figure"),
        PyTest("tests/test_figure_route.py::test_storage_being_down_is_503_rather_than_404"),
        PyTest("tests/test_figure_route.py::test_no_route_serves_the_uploaded_file"),
        PyTest("tests/test_figure_route.py::test_the_cache_lifetime_is_long_and_private"),
        PyTest("tests/test_figure_citations.py::test_a_cited_page_resolves_the_figures_printed_on_it"),
        PyTest("tests/test_figure_citations.py::test_a_citation_with_no_figure_carries_no_key"),
        PyTest("tests/test_figure_citations.py::test_another_users_figure_is_never_resolved"),
        Fidelity("FR-CIT-07 figure under the citation"),
    ),
    "Retrieval default (Rev 0.5)": (
        PyTest("tests/test_fusion.py::test_agreement_between_arms_beats_a_single_arms_top_hit"),
        PyTest("tests/test_router.py::test_every_class_routes_the_way_fr_ret_03_says"),
        PyTest("tests/test_router.py::test_every_provider_failure_yields_the_fr_ret_03_default"),
    ),
    "Grounding top-K (Rev 0.14, R-47)": (
        Default("app.config:RerankSettings", "top_k", 8),
        Default("app.config:RetrievalSettings", "merged_top_k", 50),
    ),
    "Conversation persistence (Rev 0.5)": (
        PyTest("tests/test_checkpointer.py::test_thread_id_is_the_conversation_id"),
        PyTest("tests/test_checkpointer.py::test_the_memory_backend_is_refused_in_production"),
    ),
    "Message action bar (Rev 0.5)": (
        Source("frontend/src/chat/MessageActions.tsx", "Regenerate"),
        Fidelity("§9 message action bar"),
    ),
    "Login copy (Rev 0.6)": (
        Source("frontend/src/auth/copy.ts", "Sign in to your knowledge base"),
        Source("frontend/src/auth/copy.ts", "Invalid email or password."),
        Fidelity("§9 login copy"),
    ),
    "User menu / password modal (Rev 0.6)": (
        Source("frontend/src/auth/copy.ts", "Change password"),
        Fidelity("§9 user menu"),
    ),
    "SSE frame envelope *(Rev 0.24, R-57(5))*": (
        PyTest(
            "tests/test_openapi_contract.py::test_every_frame_is_discriminated_by_its_event_name"
        ),
        PyTest("tests/test_openapi_contract.py::test_the_three_sse_routes_are_streams"),
    ),
    "Chat stream events *(Rev 0.24, R-57(5))*": (
        PyTest(
            "tests/test_openapi_contract.py::test_the_stream_item_schema_carries_the_frame_union"
        ),
        PyTest("tests/test_chat_api.py::test_a_turn_streams_progress_then_the_verified_answer"),
    ),
    "Document stream events *(Rev 0.8/0.24)*": (
        PyTest(
            "tests/test_document_events.py::test_the_snapshot_frame_carries_the_metadata_only_dto"
        ),
        PyTest("tests/test_document_events.py::test_a_change_is_framed_as_one_document_event"),
        PyTest("tests/test_document_events.py::test_a_removal_is_framed_as_an_id_only"),
    ),
    "Turn stages / outcomes *(Rev 0.24, R-57(5))*": (
        Vocabulary(
            "app.api.events:TurnStage",
            ("preparing", "retrieving", "generating", "verifying"),
        ),
        Vocabulary(
            "app.api.events:TurnOutcome",
            ("answered", "abstained", "blocked", "error", "review"),
        ),
        PyTest(
            "tests/test_openapi_contract.py::test_the_stage_vocabulary_matches_the_chat_service"
        ),
        PyTest(
            "tests/test_openapi_contract.py::test_the_outcome_vocabulary_matches_the_graph_state"
        ),
    ),
    "Error body *(Rev 0.24, R-57(4))*": (
        Constant("app.rag.errors:CONTEXT_WINDOW_EXCEEDED_CODE", "CONTEXT_WINDOW_EXCEEDED"),
        Constant("app.rag.errors:NOT_LATEST_ANSWER_CODE", "NOT_LATEST_ANSWER"),
        PyTest("tests/test_openapi_contract.py::test_the_error_codes_match_their_constants"),
    ),
    "Generated client *(Rev 0.24, R-57(1)/(2))*": (
        PyTest("tests/test_openapi_contract.py::test_the_committed_document_matches_the_app"),
        Source("frontend/package.json", "openapi-typescript"),
    ),
}


# --- §5: the NFR checklist ---------------------------------------------------------------


class Disposition(StrEnum):
    """What kind of claim is being made about an NFR — not how strong the claim is.

    ``MET_BY_CONSTRUCTION`` is not a weaker ``MET_BY_TEST``: it is the disposition for a property
    that has no behaviour to drive, where the evidence is that the code path does not exist.
    """

    MET_BY_TEST = "met_by_test"
    MET_BY_CONSTRUCTION = "met_by_construction"
    ACCEPTED_EXCEPTION = "accepted_exception"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class NfrRow:
    disposition: Disposition
    evidence: str


def _met(evidence: str) -> NfrRow:
    return NfrRow(Disposition.MET_BY_TEST, evidence)


def _construction(evidence: str) -> NfrRow:
    return NfrRow(Disposition.MET_BY_CONSTRUCTION, evidence)


def _accepted(evidence: str) -> NfrRow:
    return NfrRow(Disposition.ACCEPTED_EXCEPTION, evidence)


def _open(evidence: str) -> NfrRow:
    return NfrRow(Disposition.OPEN, evidence)


#: Every §5 requirement, in §5's order. `OPEN` rows must name the spec item that is still open —
#: guarded, because "open" with no referent is how a question stops being findable.
NFR_DISPOSITIONS: dict[str, NfrRow] = {
    # 5.1 Visual fidelity & design tokens
    "NFR-VIS-01": _met("frontend/fidelity headed in both themes; nine *.css.test.ts source guards"),
    "NFR-VIS-02": _met("styles/tokens.test.ts colour blocks; fidelity '§9 palette + tokens'"),
    "NFR-VIS-03": _met("fidelity '§9 fonts', including the rendered-width probe vs a fallback"),
    "NFR-VIS-04": _met("styles/tokens.test.ts radius scale; per-component token guards"),
    "NFR-VIS-05": _met(
        "tokens.test.ts motion block; the CSS-Modules keyframe ban; '§9 reduced motion'"
    ),
    # 5.2 Security
    "NFR-SEC-01": _met("tests/security route matrix, gate asserted by two independent oracles"),
    "NFR-SEC-02": _met("tests/security foreign-owner cells are 404; scenarios row 8"),
    # R-90(1): accepted, not met. Access control at rest is provided and hashing left the
    # application at R-28, but TLS and the *recommended* encryption at rest are both operator
    # responsibilities the deployment documents rather than provides - no application code can
    # honour either. Claiming `met` here would be the overclaim this register exists to prevent.
    "NFR-SEC-03": _accepted(
        "hashing by construction (R-28, no password column); TLS and at-rest encryption are "
        "operator responsibilities documented in docs/DEPLOYMENT.md §7-§8 (R-90(1)); egress "
        "prohibited by R-86(3)"
    ),
    "NFR-SEC-04": _met(
        "realm policy length(12)+notUsername+history(3), no rotation; lockout 30/900s"
    ),
    "NFR-SEC-05": _met("tests/security/test_injection.py structural band; scenarios row 9"),
    "NFR-SEC-06": _met("scenarios row 8 — the predicate is evaluated in-query, every turn"),
    "NFR-SEC-07": _met("tests/security/test_rate_limits.py; thresholds provisional per §8.4"),
    "NFR-SEC-08": _met("tests/test_audit.py; the T-608 operator row (DOCUMENT_REPLACE + rebuild)"),
    # By construction rather than by test, because what the requirement asks for is the
    # *absence* of an alternative: there is no in-process recognition engine to isolate from,
    # and no destination but the configured local sidecar. `tests/test_ocr.py` asserts both
    # absences, which is the only form an isolation claim can take (T-217, R-88 §8.78).
    "NFR-SEC-09": _construction(
        "app/services/ocr.py reaches deployment/ocr over a local socket; tests/test_ocr.py "
        "guards no in-process engine and no third-party destination"
    ),
    # Built and closed at T-717. It was `OPEN` from T-713 until T-715 shipped the route, which is
    # the register working - but the entry then sat for two tasks still saying "T-715 builds the
    # route" after T-715 had, which is the register rotting. The disposition and the sentence
    # under it move together (R-56).
    #
    # The evidence leads with the administrator case deliberately: NFR-SEC-10 says *the same
    # predicate as FR-RET-04*, which has no administrator branch, so this is the one route under
    # /documents that does not widen - and the four beside it do.
    "NFR-SEC-10": _met(
        "tests/test_figure_route.py — owner-only with an administrator 404, inline only, 404 for "
        "deleted/non-ACTIVE/superseded and 503 (never 404) when storage is down; the "
        "tests/security/ matrix row pins admin_widens=False"
    ),
    # The policy is configuration rather than code, so the evidence is the guard that reads
    # `deployment/nginx/` back — a header nothing checks is a header that drifts.
    "NFR-SEC-11": _met(
        "tests/test_security_headers.py — the policy is read from deployment/nginx/security.inc, "
        "its script hash recomputed from frontend/index.html, and its application scoped to the "
        "three SPA locations and away from Keycloak's own"
    ),
    # 5.3 Capacity & performance
    "NFR-CAP-01": _met("§9 defaults pinned in tests/test_budget.py and tests/test_upload.py"),
    "NFR-PRF-01": _met("StatsPanel.test.tsx advances 999ms for no tick, then 1ms for one"),
    "NFR-PRF-02": _met(
        "test_chat_api streams stage frames then one verified answer; (D) declined R-49(3)"
    ),
    # Measured rather than assumed: `set_node_defaults(timeout=...)` in `build_graph` bounds
    # every node, `retrieve` included, and expiry lands in `finalize` as an FR-ERR-04 class -
    # which is the behaviour this requirement describes. The value is the graph node timeout.
    "NFR-PRF-03": _met(
        "GRAPH_NODE_TIMEOUT_SECONDS=120 bounds retrieve; expiry is an FR-ERR-04 class"
    ),
    "NFR-PRF-04": _met(
        "test_another_users_turn_does_not_gate_me; the matrix drives 8 principal classes"
    ),
    # 5.4 Usability
    "NFR-USE-01": _met("theme.test.ts — dark on anything absent, invalid or unreadable"),
    "NFR-USE-02": _construction("hover treatments are per-component specs, not a blanket rule"),
    "NFR-USE-03": _met("Composer.test.tsx Enter-sends and Escape-closes"),
    "NFR-USE-04": _met("the three empty states asserted verbatim; fidelity '§9 empty states'"),
    "NFR-USE-05": _met("fidelity '§9 scrollbar width' — 8px, headed, both engines (R-60)"),
    # 5.5 Observability & auditability
    "NFR-OBS-01": _met(
        "tests/test_turn_telemetry.py — one row per turn that ran, from one TurnRecord"
    ),
    "NFR-OBS-02": _construction(
        "identity, not agreement: the displayed numbers are the stored columns"
    ),
    "NFR-OBS-03": _met("tests/test_citations.py; evaluation scores are a column on the message"),
    "NFR-OBS-04": _met("retention defaults pinned; the zero-horizon polarity has its own test"),
    "NFR-OBS-05": _met(
        "one OTel span per closed turn, off by default; LangSmith/Langfuse declined"
    ),
    # 5.6 Compatibility
    "NFR-CMP-01": _met(
        "BROWSER_TARGET pinned in vite.config.ts and guarded by build-target.test.ts"
    ),
    "NFR-CMP-02": _accepted("v1 is Google Drive only; the mechanism is provider-agnostic (R-63)"),
    "NFR-CMP-03": _construction("zero image assets are tracked under frontend/; icons are glyphs"),
    # 5.7 Reliability, availability & scale
    "NFR-REL-01": _met("scenarios rows 5/6/12; the hand-built arq retry, backoff and dead-letter"),
    "NFR-REL-02": _met("tests/test_health.py and tests/test_readiness.py, both probes"),
    "NFR-REL-03": _construction(
        "no process-local state on the request path; durability is the checkpointer"
    ),
    "NFR-REL-04": _met("all twelve §11 scenarios automated in tests/scenarios (T-601)"),
    # 5.8 Accessibility
    "NFR-A11Y-01": _met("tokens.test.ts reduced-motion block incl. the --motion-dot exception"),
    "NFR-A11Y-02": _met("the repo-wide outline:none ban with its three enumerated exceptions"),
    "NFR-A11Y-03": _met(
        "oxlint jsx-a11y; per-component role and label assertions; Composer.test.tsx pins the "
        "R-96 shape (no combobox role and no aria-expanded on the textarea, asserted together)"
    ),
    "NFR-A11Y-04": _met(
        "keyboard paths asserted per surface; both role=menu popovers dismiss on Tab"
    ),
    "NFR-A11Y-05": _met("live-region assertions for answer arrival and document status"),
    "NFR-A11Y-06": _accepted(
        "enumerated contrast exceptions; axe finds no other WCAG AA violation"
    ),
}


# --- what acceptance could not close -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResidualGap:
    """One thing the review found and did not close, and where it is filed.

    Filed, not merely noted: an unowned gap is indistinguishable from an oversight, and the
    register is what `tools/acceptance.py` prints rather than a human remembering.
    """

    item: str
    detail: str
    filed_as: str


#: The T-606 findings. Order is by how much a reader should care, not by section.
#: **Empty, and it is meant to stay hard to empty.** T-606 filed eight; T-613 and T-614 built two
#: of the missing instruments; R-90 took the last three as *decisions* rather than work, because
#: that is what they had always been — NFR-SEC-03's final clause is an operator responsibility the
#: deployment documents (R-90(1), and the row above is `accepted`, not `met`, for exactly that
#: reason), the audit trail is kept indefinitely with no pruning mechanism (R-90(2)), and the
#: sidebar list is unbounded by decision (R-90(3)).
#:
#: **Adding a row back is the honest move whenever something is filed but not fixed.** The rule
#: this tuple exists to enforce is that a gap has an owner and a name; an empty list is a claim
#: that nothing is outstanding, so it must never be emptied by re-describing a gap as a feature.
#: R-90(3) is the cautionary case: the entry it replaced closed its own gap by citing behaviour
#: the code does not have ("the conversations route pages" — it returns a bare, unpaged array).
RESIDUAL_GAPS: tuple[ResidualGap, ...] = ()
