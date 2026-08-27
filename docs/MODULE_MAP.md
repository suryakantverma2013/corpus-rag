# Module map

Where the code lives, what each package owns, and — the part that matters most — **what each one
must not do, and what stops it**.

This is written by hand on purpose. A generator can render every docstring in the tree, and this
project has one for exactly that (below). What a generator cannot tell you is that
`app/api/` is forbidden to import `app/ingestion/parsers/`, or that seven modules in `app/rag/`
must never import `langgraph`, or that those are not conventions but **test failures**. Architecture
lives in the negative space, and the negative space has no docstring.

> **Generated references** (not committed — regenerate, never edit):
>
> ```bash
> cd backend && uv run python -m tools.apidocs    # every backend docstring, cross-linked
> cd frontend && npm run docs                     # the GUI's components, hooks and stores
> ```
>
> Both write to `docs/reference/`. The HTTP surface is different: it *is* committed, as
> [HTTP_API.md](HTTP_API.md), because `docs/` is the only documentation the public repository
> ships and a reader who clones should not need a toolchain to read the API. It is kept honest by
> a drift test rather than by discipline.

For how the pieces fit together at runtime see [ARCHITECTURE.md](ARCHITECTURE.md); for the tables
they read and write see [DATA_MODEL.md](DATA_MODEL.md).

**How to read an entry.** *Owns* is the responsibility. *Must not* is the constraint that would
otherwise erode. *Enforced by* names the test that fails when it does — where there is one. An
entry with no *Enforced by* is a convention, and should be read as weaker.

---

## Backend

### `app/` — 8 modules
`config` `logging_config` `logging_context` `main` `openapi` `runtime` `tokens` `tracing`

**Owns.** Process-level concerns: settings (29 nested models), the FastAPI app factory, structured
logging and its per-turn context binding, the OpenAPI document, and the OpenTelemetry span.

**Must not.** `config.py` must not gain a setting whose off state removes a requirement — the
project ships `EVAL_ENABLED` (degrades to "no chips", which the requirement permits) and
deliberately no `GATE_ENABLED` (its off state would serve ungrounded answers).

**Notes.** `runtime.py` exists for one Windows fact: psycopg's async driver needs `add_reader`,
which the default `ProactorEventLoop` lacks. `tokens.py` is the `ceil(len/4)` estimator, and it has
a **second implementation in the browser** that must agree.

**Enforced by.** `tests/test_env_templates.py` (every setting documented, both templates boot),
`frontend/src/tokens.test.ts` (reads `app/tokens.py` off disk, across languages).

### `app/api/` — 14 modules
`admin` `audit` `auth` `cloud` `config` `conversations` `documents` `errors` `events` `health`
`jobs` `messages` `router` `users`

**Owns.** Every HTTP route, its status codes, and its error envelope. 38 operations.

**Must not.** Import the parsers, the chunker, the incremental pipeline, the scanner or the worker.
Extraction is CPU-bound C code running on hostile input; the upload endpoint has to stay responsive
enough to return its `202`. This is the single most-guarded boundary in the backend — **four
separate tests**, one per module, because it is the one that would erode quietly.

Must not declare a status it cannot return (a real defect once: `PATCH`/`DELETE /users/{id}`
declared `403` for a refusal they answer `409`).

**Seams.** Everything reaches the database through `app/db/repositories/` and the outside world
through `app/services/`.

**Enforced by.** `tests/test_parsers.py::test_no_api_module_imports_the_parsers`,
`test_chunker.py`, `test_incremental.py`, `test_ingest_task.py` (scanner and worker),
`tests/security/` (a 38-row route manifest — a new endpoint fails the suite until someone declares
who may call it), `tests/test_openapi_contract.py`.

### `app/auth/` — 9 modules
`dependencies` `jwks` `keycloak_client` `principal` `roles` `schemas` `service` `tokens`
`users_service`

**Owns.** RS256/JWKS validation, ROPC token exchange against Keycloak, the role dependencies, and
the local mirror of a Keycloak subject.

**Must not.** Hash, store or verify a password. Keycloak owns credentials (R-28) and `users` has no
`password_hash` column — the constraint is met by construction rather than by review.

**Notes.** Two clocks: `is_active` is read from the database every request and revokes immediately;
**the role is a token claim**, so a demoted administrator keeps it until the token expires (~300 s).
To remove access now, disable the account.

**Enforced by.** `tests/security/` (the cross-principal matrix), `tests/test_auth_live.py` (imports
the committed realm into a throwaway realm and reads the mapping back — Keycloak ignores realm keys
it does not recognise, *silently*).

### `app/db/` — 3 modules + `models/` (12) + `repositories/` (14)
`base` `enums` `session`

**Owns.** The async SQLAlchemy engine, the ORM models registered on `Base.metadata`, and every
query in the system.

**Must not.** Anything outside `repositories/` must not write SQL. `document_chunks` must not grow
a column nothing writes — one did (`is_active`), and its predicate was a tautology on every
retrieval query until it was dropped.

**Notes.** Alembic's `env.py` is excluded from the generated reference: it imports
`alembic.context`, a proxy that exists only while Alembic is driving.

**Enforced by.** `tests/test_openapi_contract.py`, and the migration round-trip tests.

### `app/ingestion/` — 4 modules + `parsers/` (9)
`chunker` `figures` `incremental` `scanner` · parsers: `base` `csv` `docx` `figures` `markdown`
`pdf` `recognition` `tables` `text`

**Owns.** Bytes to chunks: format sniffing, extraction, optional recognition (OCR), table
structure, splitting, and the incremental diff that decides what gets re-embedded. Also optional
figure extraction, which is **not** part of that pipeline — see below.

**Must not.** Run in the API process (see `app/api/`). Cut a chunk across a `ParsedBlock`. Synthesise
a page number for a format that has no pagination — DOCX and Markdown carry section locators
instead, because a "p. 7" the user cannot verify is worse than no locator. **Put a figure on
`ParsedDocument`**: that dataclass is the fingerprint-bearing contract, and everything on it
describes the text a chunk is built from. `parsers/figures.py` detects and renders; `figures.py`
runs the extraction pass as a **second open of the same bytes**, so "a figure feeds no embedding"
is unrepresentable rather than remembered.

**The constraint that governs changes here.** `embedding_fingerprint` folds in the embedding model,
the chunker's sizing knobs and `PREPROCESSING_VERSION`. Change any of them and every stored chunk
is stale — correctly, and invisibly. That is why OCR must be **byte-reproducible**: a recogniser
that varies run to run leaves fingerprint reuse permanently empty, so every replace, retry and
rebuild re-embeds the whole document. It is also why `tools.reembed` exists.

**Enforced by.** `tests/test_chunker.py`, `test_parsers.py`, `test_recognition.py`,
`test_incremental.py`, `test_figures.py`, `test_figure_extraction.py`.

### `app/rag/` — 17 modules
`budget` `citations` `errors` `evaluation` `fusion` `generation` `graph` `groundedness` `history`
`prefetch` `prompts` `rerank` `retrieval` `router` `search` `state` `telemetry`

**Owns.** The LangGraph turn: screen → route → retrieve → rerank → generate → gate → finalize.

**Must not.**
- `state.py` must not import `langchain_core`, and its field list is **frozen** — additive changes
  are safe, renames orphan mid-turn conversations.
- Seven modules must not import `langgraph`, each with its own test: `generation`, `groundedness`,
  `history`, `prompts`, `rerank`, `router`, `search`. They are called from outside the graph
  (routes, workers, tests), and `graph.py` applies msgpack patches at import time.
  `errors.py`, `budget.py`, `citations.py` and `evaluation.py` state the same property in their
  docstrings and **have no test for it** — a convention, not a guard, and worth converting if one
  of them ever grows a graph import.
- Nothing may put document text into a checkpointed channel (FR-PER-03).
- Nothing may re-derive a citation's grounding set: `[S<n>]` resolves positionally against the list
  the prompt composer emitted, or it validates against the wrong set *and passes*.

**Failure directions are deliberate and alternate.** `screen`, `retrieve`, `generate` and the gate
fail **closed**; `route` and `rerank` fail **open**, because each has a defensible degraded output.
Changing one is a design decision, not a refactor.

**Enforced by.** `tests/test_graph.py` (frozen field names, per-turn reset, `adapt` is the only
writer of `retry_count` and the only way back into `retrieve`), plus one `imports_no_langgraph`
test per module.

### `app/security/` — 3 modules
`content_validation` `prompt_injection` `rate_limit`

**Owns.** Query screening, upload content validation, and the slowapi limiter.

**Must not.** Screen *retrieved* text. The chunk is already inside the caller's own scope, and
blocking it would make a document the user owns permanently unanswerable. Retrieved text is
neutralised and fenced, never rejected.

The structural control — instructions in the `system` message, untrusted content fenced in another
role, query last — **must never acquire a feature flag**. `GRAPH_SCREEN_ENABLED` exists only
because the structural half cannot be switched off.

**Notes.** The limiter fails **open**: if Redis blips, limits stop applying rather than locking
everyone out. Its buckets are keyed explicitly — a default `key_style="url"` once made the bucket
the concrete path, so a caller with N documents got N × the budget.

**Enforced by.** `tests/security/test_injection.py` (a committed evasion corpus; it asserts
*structure*, not detection rate), `tests/security/` rate-limit bucket rows.

### `app/services/` — 21 modules
`audit` `chat` `checkpoint_retention` `checkpointer` `clamav` `cloud_import` `cloud_links`
`conversations` `document_events` `documents` `drive` `embeddings` `figures` `health` `jobs` `llm`
`model_selection` `object_storage` `ocr` `processing_lock` `reembed`

**Owns.** Every boundary to something outside the process: object storage, the model provider,
ClamAV, the OCR sidecar, Keycloak brokering, Google Drive, the job queue, and the LangGraph
checkpointer.

**Must not.** Leak a vendor exception past the seam. `object_storage` translates *both* botocore
error families — a down daemon raises `EndpointConnectionError`, a sibling of `ClientError`, not a
subclass, and missing it turned a `503` into a `500` on the one failure the seam exists to abstract.

**Seams worth knowing.** `llm.ChatClient` and `embeddings.EmbeddingClient` are protocols with fake
backends, which is what lets ~2,000 tests run with no provider key. A second entry point inherits a
pipeline by satisfying a protocol rather than by copying it: cloud import reaches the ingestion path
because `upload_document` takes a `ByteSource`, so dedup, quota, limits and scanning apply by
construction.

`health.py` is the reason there are **two** readiness endpoints — a dead scanner or OCR sidecar
must pull the worker out of service without pulling the chat surface out with it.

**Enforced by.** `tests/test_llm.py`, `test_cloud_import.py`, `tests/test_evals.py`
(all DeepEval access stays behind the evaluation seam, AST-checked).

### `workers/` — 7 modules
`common` `delete` `evaluate` `ingest` `main` `retention` `sweeper`

**Owns.** The arq worker: ingestion, deletion, post-hoc evaluation, and three crons (undispatched-job
sweep, checkpoint pruning, telemetry retention).

**Must not.** Reverse the two purge orderings, which are opposite **on purpose**:
- **Delete** purges objects *before* the commit that marks `DELETED` — a reader must never see a
  document claimed gone while its bytes are still there. A crash between them orphans bytes.
- **Replace** purges the superseded version *after* the swap commit — v(n) is still serving until
  v(n+1) lands.

A purge that exhausts its retries must never write `FAILED`: that state renders a Retry affordance
that would re-*ingest* a deleted document.

**Notes.** These two orderings are also why a *hot* backup cannot be made safe in one direction —
see [DEPLOYMENT.md §9.2](DEPLOYMENT.md).

**Enforced by.** `tests/test_ingest_task.py`, `test_delete_task.py`, `tests/scenarios/` (the twelve
§11 production scenarios — the only tests that cross route → worker → route).

### `tools/` — 7 modules
`acceptance` `apidocs` `feedback_calibration` `httpdocs` `reembed` `set_model` `spec_xref`

**Owns.** Operator and repository tooling: neither application code nor tests, excluded from the
wheel.

**The shape they share.** Where a rule is objective it is a *test*; where it needs judgement it is a
*report* that always exits 0. `spec_xref` and `acceptance` are the pattern — a hard test for "every
marker cites an open issue", a printed report for "here is what covers what".

**Must not.** Print non-ASCII to stdout. A Windows console is `cp1252` and a 👍 in a report crashes
it; prose keeps the character, stdout does not.

### `evals/` — 4 modules
`corpus` `pipeline` `report` `run`

**Owns.** The offline golden-set harness: a fixed authored corpus scored with reference-based
metrics that never reach a user surface.

**Must not.** Enable judge escalation. Selective re-judging is a biased estimator — fine for a chip
a user reads, disqualifying for the instrument doing the measuring.

**Enforced by.** `tests/test_evals.py` (both the seam and the escalation call site, AST-checked).

---

## Frontend

### `src/api/` — 5 files
`auth` `client` `detail` `index` `schema.d.ts`

**Owns.** The generated OpenAPI types, the fetch client, token attachment and refresh.

**Must not.** Rebuild a `Request`'s headers. `openapi-fetch` calls `fetch(Request)`, so a shim
written as `fetch(url, {...init, headers})` replaces them wholesale — which strips a multipart
boundary and surfaces as a `422` that looks nothing like a header problem. The middleware is
`request.headers.set(...)`: **additive, never replacing**.

`schema.d.ts` is generated — `npm run build` regenerates it before `tsc`, so a stale copy cannot
reach a build.

### `src/auth/` — 9 files
`AuthContext` `AuthProvider` `ChangePasswordModal` `copy` `identity` `useAuth` …

**Owns.** Session lifecycle, the login screen, change-password.

**Must not.** Renew on a timer — renewal is request-driven, or the SSO idle timeout silently stops
existing. And "any 401 returns to login" is a defect read literally: `change-password` answers 401
for a wrong *current* password. The exempt list is deliberately **not** the list that skips renewal.

**Notes.** The refresh token is an httpOnly cookie and nothing else; `TokenResponse` has no
`refresh_token` field, so a body copy is unrepresentable rather than forbidden.

### `src/chat/` — 18 files
`AiMessage` `ChatHeader` `CitationCard` `MessageList` `Markdown` `mutations` `useChat` …

**Owns.** The transcript: message rendering, the in-tree Markdown renderer, citation chips and
their hover card, regenerate, feedback.

**Must not.** Form a markup string. The renderer emits React elements, so "sanitized" holds *by
construction* — there is no path from a `<` in the content to an element, and nothing calls
`dangerouslySetInnerHTML`.

Chips are anchored **by offset** into the concatenated text, never by a sentinel, so content cannot
forge one and a list straddling a segment boundary stays one list.

**Notes.** The turn must clear in a `finally` — there are four exits, and clearing only on the happy
path deadlocks the composer *and* the KB modal.

### `src/composer/` — 3 files
`Composer` `MentionMenu` `mentions`

**Owns.** The input, the token budget block, and the `@`-mention menu.

**Must not.** OR the mention-open state with its previous value. It is an **assignment** every
keystroke, which is what closes a button-opened menu on the next one.

**Notes.** The menu opens with **no active option**, which is how Enter can mean *send* (a
requirement) and *select* (the listbox contract) without either default being lost.

### `src/kb/` — 7 files
`DocumentRow` `DropZone` `KnowledgeBaseModal` `documents` `mutations` `useDocuments` …

**Owns.** The knowledge-base modal, upload, the live document stream, and the four document verbs.

**Must not.** Invent a document. An optimistic row could only reconcile on filename, which is not
identity — so an upload is a *pending entry that is not a document*, handed off on the id the server
returns.

The in-flight flag must **lapse**, not wait for its own signal to fall: the case it exists for is
precisely the one where that signal never saw the turn start.

### `src/cloud/` — 5 files
`CloudImportDialog` `cloud` `mutations` `useCloudFiles` `useCloudLink`

**Owns.** Drive linking and the file picker.

**Must not.** Use `disabled` on a listbox option — an already-imported row must be `aria-disabled`,
or it leaves the interaction model a screen-reader user is navigating with.

The page cursor is a **ref, never an effect dependency**: as a dependency, each response requests
the next page and walks the user's entire Drive at 20 calls a minute.

### `src/sidebar/` — 4 files · `src/shell/` — 1 · `src/stats/` — 3
`Sidebar` `ConversationList` `conversations` `useConversations` · `AppShell` ·
`StatsPanel` `stats` `useConfig`

**Owns.** Conversation list and rename/delete; the three-column grid; the analytics cards.

**Must not.** Wrap the stats panel in a `<div>` — `AppShell` owns the column's 14px gap, and a
wrapper makes the six children one flex item and swallows every gap. jsdom cannot see it.

Derive a band and a bar width from the **rounded** score, not the raw one: `0.8951` renders `0.90`
while the raw value bands as `warn`, painting an amber bar beside a numeral that reads green.

### `src/ui/` — 2 files · `src/theme/` — 4
`Dialog` `ConfirmDialog` · `theme` `ThemeContext` `ThemeProvider` `useTheme`

**Owns.** The dialog primitive (focus trap, Escape, restore) and runtime theming.

**Must not.** Handle Escape with a bare `document` listener. Dialogs **nest**, two capture-phase
listeners fire in registration order, and `stopPropagation` does not stop a sibling — so `Dialog`
keeps a module-scope stack. Nesting is a fact about what is mounted, not something a call site
declares.

Theme must not consult `prefers-color-scheme` (it would contradict the dark default) and is applied
by a pre-paint inline script.

### `src/styles/` + `src/tokens.ts`
`tokens.css` · `tokens.ts`

**Owns.** The design tokens, and the browser copy of the token estimator.

**Must not.** Name a `@keyframes` from inside a `*.module.css`. CSS Modules rewrites
`animation-name` **even for a keyframe it does not declare**, so the animation compiles to a hashed
name matching nothing and silently never runs — six modules were dead this way. Use the global
`.animate-*` utilities.

Must not write a per-component `prefers-reduced-motion` block: the keyframes are redefined inside
the media query centrally, and a local `animation: none` falls back to the element's *base* style,
which for the typing dots is invisible.

**Enforced by.** `frontend/src/tokens.test.ts`, the `*.css.test.ts` source guards, and
`frontend/fidelity/`.

### `src/test/` — 3 files
`css-source` `setup` `transcripts`

**Owns.** Test-only fixtures and helpers.

**Must not.** Be importable from shipping code — `tsconfig.app.json` excludes it, so a component
that imports a fixture fails the build rather than shipping one.

---

## The guards, in one place

Documentation rots; these do not, because they fail.

| Guard | Asserts |
|---|---|
| `tests/security/` (38-row route manifest) | every route declares who may call it; two independent oracles for the auth gate |
| `tests/acceptance/` | every §9 requirement row carries evidence; shipped literals read back |
| `tests/scenarios/` | the twelve production scenarios, route → worker → route |
| `tests/test_openapi_contract.py` | `openapi.json` still matches the app |
| `tests/test_http_docs.py` | `docs/HTTP_API.md` still matches `openapi.json` |
| `tests/test_env_templates.py` | both env templates match the settings model, and boot |
| `tests/docs/` (21-document manifest) | every link, anchor and section reference in the published documentation resolves; every variable, route and command it names exists |
| `tests/test_spec_xref.py` | no `TBD` marker cites a closed issue |
| `tests/test_graph.py` | `RAGState` field names frozen; per-turn reset covers every channel |
| `test_no_api_module_imports_*` (x4) | the API/ingestion boundary |
| `*_imports_no_langgraph` (x7) | the seven modules callable from outside the graph |
| `frontend/fidelity/` | computed-style checks over every surface, both themes, headed |
| `frontend/a11y/` | ten surfaces x two themes; no new WCAG violation outside the enumerated exceptions |

**One convention the last of those imposes on whoever writes documentation.** A `§` reference is
resolved against the document it sits in, or against the document its sentence names —
`DEPLOYMENT.md §9.2`, `[SECURITY.md §11.3](SECURITY.md)`. So name the file whenever the section is
not this file's, and write the word **spec** before a number that belongs to the specification
(`spec §8.80`), which cannot be checked because the specification does not ship. Both readings
serve a reader who cannot open the spec at all, which is why the guard asks for them.
