# Acceptance

What was verified before Corpus was called done, how, and — as importantly — what is still open.

This is the record of the acceptance review. It is written rather than generated, but the two
things it would be easiest to get wrong are both generated: the coverage map is a committed
manifest with a test that fails when a pointer stops resolving, and the residual-gap list below is
printed from that same manifest, so a gap cannot be quietly closed in prose while the register
still carries it.

```
cd backend && uv run python -m tools.acceptance      # the report
cd backend && uv run pytest tests/acceptance         # the rule the report cannot enforce
```

## 1. What was checked

The requirements come in two shapes and are checked differently.

**Acceptance-critical literal values** — 60 rows covering the design tokens, the copy strings, the
route and response contracts, the closed vocabularies and the numeric budgets. Each is mapped to
one or more evidence pointers in `backend/tests/acceptance/`. Six kinds:

| Pointer | Resolves by | Fails when |
|---|---|---|
| `PyTest` | parsing the module for the named test | the test is deleted or renamed |
| `Default` | reading the field default off the settings model | the shipped default changes |
| `Constant` | importing the module attribute | normative copy or an error code is reworded |
| `Vocabulary` | expanding the live enum or `Literal` | a member is added, removed or renamed |
| `Source` | reading a source file for the literal | a copy string is edited |
| `Fidelity` | the surface exists in the fidelity harness | a rendered check is dropped |

`Default`, `Constant` and `Vocabulary` are second oracles: they compare the value the product
ships, so they fail on a changed literal even when every behavioural test still passes. That
matters, because four of them were previously pinned nowhere at all — the grounding top-K, the
context-window budget, the upload size and quota limits, and the two normative failure strings.

The three most recent rows are Rev 0.55's: the recognition policy (off by default, its DPI,
confidence floor, page ceiling and budget), the tabular-structure floors, and
`PREPROCESSING_VERSION`. The last is a second oracle twice over, and deliberately so — it is an
input to the embedding fingerprint, so it is one of the few literals in the product whose value
being wrong costs a full re-embed of the corpus rather than a failing assertion.

**Non-functional requirements** — all 42, each carrying one of four dispositions:

- **met by test** (34) — a named test or harness check fails if the property stops holding;
- **met by construction** (5) — there is no behaviour to drive because the code path does not
  exist; hover treatments are per-component rather than blanket, the displayed token counts *are*
  the stored columns rather than a second store agreeing with them, no image asset is tracked
  anywhere under `frontend/`, no request-path state lives in a process, and recognition has no
  in-process engine to isolate from and no destination but the configured local sidecar;
- **accepted exception** (2) — see §3;
- **open** (1) — see §4.

The two registers are cross-checked, so the security requirements have one owner rather than two
copies that drift apart — the same eight ids on both sides, and every requirement still open here
must be one the security package records as deferred. The relation is **containment, not
equality**, and the difference matters: a deferral says *this package has no request-path surface
to drive*, while open says *nothing anywhere covers it*. Those coincided until the password policy
landed, which is covered by an offline guard and is still not that package's job.

## 2. What was run

Every instrument, on one tree, against real services.

| Instrument | Result |
|---|---|
| Backend suite | **1,894 passed, 0 skipped** (Postgres, Redis, MinIO, ClamAV, Keycloak, OpenAI all live) |
| Frontend unit suite | **1,141 passed** (57 files) |
| Lint + build | `oxlint` clean; `tsc -b && vite build` clean |
| Visual fidelity, **headed**, both themes | **197/197 checks, exit 0** |
| End-to-end journey | **1 passed** in 23.9 s — sign in → upload → ask → cite → rate → regenerate → rename → delete |
| Containerized stack | all three probes `200` with every dependency ok; SPA served; API `401` unauthenticated; `/docs` and `/openapi.json` fall through to the SPA rather than reaching the API; Keycloak proxied under `/auth/` |

Two notes on how to read that table.

**The backend suite is 0 skipped only with Keycloak up.** With it down the run was *3 failed, 12
skipped* — and the three failures were `503`s from the user-management routes, which is the
documented meaning of that status (unreachable, not forbidden). A skipped live test is not a
passing one; if this suite ever reports skips, start the identity provider before reading anything
else into it.

**The containerized stack was verified running, not rebuilt from empty.** The three probes,
routing and SPA delivery were checked against the live stack; a `down -v` rebuild was deliberately
not performed, because it destroys that stack's volumes and the from-scratch bring-up time is
already on record. If you need the cold-start figure, take it yourself — it is about a minute.

## 3. Accepted exceptions

Two requirements are met with a stated exception rather than in full. Both are decisions, not
omissions, and both are enumerated so that a conformance answer can quote them.

**Colour contrast (NFR-A11Y-06).** The palette is fixed by the visual-fidelity requirements and
fails WCAG 2.2 AA on an enumerated set of pairs — muted secondary text everywhere, white on the
accent in dark, the three evaluation hues as text in light, and hairline borders. The mitigation
is normative and is checked per component: **colour is never the sole carrier of information**, and
no new pair may drop below threshold. Outside that enumerated set, an axe pass over ten surfaces in
both themes finds no other AA violation.

**Cloud-drive interoperability (NFR-CMP-02).** One provider ships. The mechanism is
provider-agnostic — the token is brokered by the identity provider and Corpus stores no
third-party credential — but the requirement's plural is not yet earned.

Two further deviations are recorded at their own requirements rather than here, because they are
invisible to a user and were argued at the time: the sparse retrieval arm is PostgreSQL full-text
ranking rather than BM25, and reranking is batched pointwise scoring rather than a cross-encoder.

## 4. What is still open

Printed from the manifest by `python -m tools.acceptance`; summarised here.

**One requirement is open**, and it was narrowed to a single clause rather than left vague.
Password hashing left the application entirely when identity moved to Keycloak — there is no
password column to hash. TLS is an operator responsibility the deployment *documents* rather than
provides: the stack speaks plain HTTP behind its edge and expects termination in front of it. Data
egress is now settled as a **prohibition** — prompt text, completion text and retrieved document
text shall not leave the deployment, which is why the external tracing vendors remain declined on a
requirement rather than on an open question. What remains is **encryption at rest**, configured
nowhere, and a deployment decision rather than application code.

**Two findings were closed the same day.** The review found the realm carrying **no password policy
at all** — the absence of one, so any password an administrator typed was accepted — and it now
sets a length, username and history rule. What is deliberately *not* set is a rotation policy:
under backend-mediated ROPC there is no browser, so a required action locks the account out
permanently, which makes rotation an outage rather than a stricter rule. Its absence is asserted by
a test. Three provisional rejection strings were also confirmed and moved into their requirements.

**Two further gaps** are filed rather than fixed, each with an owner:

1. The audit trail has no stated retention period. Telemetry retention is settled at 90 days; the
   audit log's own period is a compliance decision nobody has taken.
2. Sidebar history pagination is deferred until conversation counts justify it.

The other two the review filed have since been closed. The uncommitted environment-variable
coverage check was closed by T-613: `backend/tests/test_env_templates.py` now diffs
`backend/.env.example` against the settings model and `deployment/.env.prod.example` against the
compose file's interpolations, both directions, and boots `Settings` from each to catch what a
name diff cannot see. The unrunnable accessibility audit was closed by T-614, which committed the
axe pass as `frontend/a11y/` — so NFR-A11Y-06's conformance claim is now a command anyone can
repeat rather than a written result.

## 5. Two findings worth generalising

The fidelity harness reported **117 of 156** checks on its first run of this review. Every failure
traced to a single cause, and the cause was in the instrument.

The cloud-import probe clicks "Add from cloud drive". With no linked account that button is not
inert — it is a full-page redirect to the identity provider's linking endpoint. The probe waited
for a picker that could never appear, recorded *"no linked Drive account — not a fidelity
failure"* as a **pass**, then tried to close a modal that was no longer on screen. The browser was
left on Keycloak's login page, and the entire light-theme pass, the motion checks, the layout
expansion and both empty states — 39 checks — measured PatternFly instead of Corpus.

Three things about that are worth keeping.

It was **invisible to the assertions and obvious in a screenshot**. The failures read as thirty-nine
unrelated regressions; one look at the image showed a login page that was not ours.

The branch that broke it **had never executed**. It was written when the audit account was linked to
a real Drive account, so the degraded path was reasoned about and never run — and the one thing it
did was report a pass.

And the fix recovered **41 checks that were never reached at all**: 156 → 197. A harness that stops
early does not report the checks it skipped, so the total is not a constant to be trusted between
runs. With a linked Drive account the full pass is 207.

---

The second is smaller and will recur. Four telemetry tests counted the **whole** `turn_telemetry`
table, which has no foreign keys and is deliberately never cleaned up by a conversation delete — a
user's deletion must not rewrite an operator's history. So every real turn ever run against the
development database is still in it, and the moment anyone runs the end-to-end suite, which the
project tells them to, those four tests start failing on rows that are supposed to be there. They
now empty the table inside their own rolled-back transaction.

The generalisation: **an acceptance run exercises the product, and exercising the product leaves
state.** A test that counts a whole table is asserting something about the machine it runs on, not
about the code. This one was found by running everything in one sitting — which is the only way it
could have been found, because each instrument on its own leaves the suite green.
