# Configuration

**This is not a list of every setting.** [`backend/.env.example`](../backend/.env.example) already
documents all 189 of them, one comment per variable, and `backend/tests/test_env_templates.py` fails
if it ever stops — in both directions, and it boots `Settings` from the template to catch a value
that parses as a name but not as a number. Copying that list here would create a second copy to
drift.

What follows is what that file cannot tell you: which settings you actually touch, what breaks when
they are wrong, and the rules the whole surface obeys.

---

## 1. A value crosses three surfaces, and the last one is the trap

| Surface | Answers | Where |
|---|---|---|
| `app/config.py` | **what exists** — 189 names, types, defaults, validators | 30 groups, each with an `env_prefix` |
| `backend/.env.example` | **what you may set** — documentation only, no runtime role | guarded both ways |
| `x-corpus-env` | **what a container actually receives** — 46 keys | `deployment/docker-compose.prod.yml` |

**Compose does not forward `--env-file` into containers.** That file populates *interpolation* only.
A setting reaches the api and worker processes **only if its name appears as a key in
`x-corpus-env`**.

So in the containerized stack: putting a setting in `deployment/.env.prod` does nothing unless the
compose file references it. And because every settings group uses `extra="ignore"`, **a name nothing
reads produces no error at all** — no warning, no startup failure. The stack comes up healthy,
running on a default, and looks correct.

> **To override an application setting in production, add it to `x-corpus-env` — not to
> `.env.prod`.**

This is the most expensive thing to get wrong here, because nothing tells you that you have.

## 2. What you actually set

Everything else has a working default. These do not, or their defaults are wrong for anything real.

### Must set — or it does not work

| Setting | What happens if you skip it |
|---|---|
| `OPENAI_API_KEY` | **Guarded.** The first client construction refuses: *"OPENAI_API_KEY is empty; set it or select LLM_BACKEND=fake"*. It never silently falls back — embedding a corpus with nonsense vectors and reporting `ACTIVE` is far worse than refusing |
| `KEYCLOAK_CLIENT_SECRET` | **Unguarded, and it has two jobs.** Every login fails at Keycloak — *and* it is the HMAC key for cloud-link state, so an empty value leaves that signature keyless with no error anywhere |

### Endpoints — defaults work locally, wrong everywhere else

`DATABASE_URL` · `MINIO_ENDPOINT` (must match the port you published) · `KEYCLOAK_SERVER_URL` (must
equal Keycloak's own `KC_HOSTNAME` *exactly, path included* — it stamps `iss` from its own
configuration, not from the URL a request arrived on, so a mismatch rejects every token while
reporting only an invalid issuer) · `KEYCLOAK_INTERNAL_URL` (leave **empty** for single-host,
including development) · `CLOUD_CALLBACK_BASE_URL` / `CLOUD_RETURN_URL`.

### The one that arms the safety net

**`ENVIRONMENT`.** It defaults to `development`, and leaving it unset is what *disarms* two boot
refusals — a non-durable checkpointer and an insecure refresh cookie both become permissible. The
production compose sets it for you; anything hand-rolled must.

### Backends — the fake/real switches

`LLM_BACKEND`, `EMBEDDING_BACKEND` (`openai` | `fake`), `SCANNER_BACKEND` (`clamav` | `structural` —
there is no "off"), `QUEUE_BACKEND` (`arq` | `none` — with `none` the job row is written and
**nothing ever dispatches**), `STORAGE_BACKEND`, `CHECKPOINTER_BACKEND`.

### Test gates

`KEYCLOAK_LIVE_ADMIN_PASSWORD` and `OCR_LIVE_TEST` are read from the environment by the tests, not by
`Settings`. Without them the corresponding tests **skip**, and a skipped test is not a passing one.

## 3. The eight boot refusals

The app declines to start rather than run a configuration that would be wrong. **Four of the eight
exist because the bad setting would otherwise be a silent no-op, not a crash** — which is the whole
argument for having them.

| Refusal | Why it is not merely a bad value |
|---|---|
| `CLAMAV_MAX_STREAM_BYTES` < `UPLOAD_MAX_FILE_BYTES` | The largest legal upload would be **unscannable**. Raise it here and in `clamd.conf` together |
| `production` + `CHECKPOINTER_BACKEND=memory` | Conversations would not survive a restart |
| `production` + insecure refresh cookie | The only copy of a renewable session credential would travel unencrypted |
| `RERANK_TOP_K` > `RETRIEVAL_MERGED_TOP_K` | Retrieval never hands the reranker that many candidates — **a larger top-K is a silent no-op** |
| `CONTEXT_ANSWER_RESERVE_TOKENS` < `LLM_MAX_OUTPUT_TOKENS` | An answer may run to that ceiling, so a smaller reserve cannot keep the conversation inside its budget |
| `GRAPH_MAX_RETRIES` > 1 | The retry composes the rejected answer as a single-shot probe; a second cycle repeats the first's inputs at full latency for nothing |
| OCR budget + timeout > `WORKER_JOB_TIMEOUT_SECONDS` | A healthy long ingestion would render as *stalled* |
| OCR budget + timeout + figure budget > `WORKER_JOB_TIMEOUT_SECONDS` | The same, for the pair: recognition and figure extraction run in **one** ingestion job, so their budgets add. At the shipped defaults that is 600 + 60 + 300 = **960s against 900** — **enabling both features without raising the job timeout is a stack that does not start** |

Roughly forty more refusals live inside individual groups — ranges, floors, closed vocabularies, and
two that exist because a wrong value would **hang a process rather than degrade it**
(`CHUNKER_OVERLAP_CHARS` ≥ `CHUNKER_TARGET_CHARS` is a non-terminating loop in the worker;
`EMBEDDING_MAX_BATCH_CHARS` below 8,000 is one in the batch planner).

## 4. `0` means opposite things

Deliberately, and it is worth knowing before you set one to zero:

| Setting | `0` means |
|---|---|
| `CHECKPOINTER_RETENTION_INTERVAL_SECONDS` | **disabled** — never sweep |
| `EVAL_ESCALATE_BELOW` | **disabled** — never escalate to the stronger judge |
| `TELEMETRY_RETENTION_DAYS` | **keep forever** |

The difference is period versus horizon. Read as a horizon, `0` would mean "delete everything older
than now" — the single value that empties the table — so it had to become the off switch instead.

## 5. Knobs that move together

Changing one of these alone produces something that looks fine and is not:

- **`RERANK_TOP_K` / `RETRIEVAL_MERGED_TOP_K`** — a refusal enforces the relationship.
- **`CLAMAV_MAX_STREAM_BYTES` / `clamd.conf`'s `StreamMaxLength`** — two processes, one limit.
- **`RETRIEVAL_FTS_LANGUAGE` / the GIN index** — the index is functional; changing the language
  needs a migration that rebuilds it, or every search silently sequential-scans.
- **Any `CHUNKER_*` value** — they compose the chunking version, which is a fingerprint input, so a
  change means **a full re-embed** of every document.
- **`PARSER_OCR_ENABLED` / the `ocr` compose profile** — two switches that must agree, and neither
  complains alone.
- **`PARSER_FIGURES_ENABLED` is the opposite case, and it is worth stating for that reason** — it is
  **one** switch with **no** compose profile, because extraction is in-process and has no sidecar.
  If you go looking for its second half you will not find one.
- **`PARSER_OCR_ENABLED` + `PARSER_FIGURES_ENABLED`** — turning both on needs
  `WORKER_JOB_TIMEOUT_SECONDS` raised with them; a refusal enforces it (§3).
- **`WORKER_JOB_TIMEOUT_SECONDS`** has four consumers: the job budget, the denominator of the
  *stalled* flag in the knowledge-base list, the OCR worst-case ceiling, and the figure budget
  that adds to it.

## 6. Settings that deliberately do not exist

Asked for often enough to be worth stating. Each absence is a decision:

`GATE_ENABLED` · `TELEMETRY_ENABLED` · `CONTEXT_ENABLED` · `PARSER_TABLE_ENABLED` · `OCR_BACKEND`

The rule they share: **an off switch is legitimate only when its off state is a degradation the
requirement sanctions.** `EVAL_ENABLED` exists because "no evaluation chips" is something the
requirement explicitly permits. Turning off grounding checks, telemetry or the conversation budget
would remove a guarantee rather than degrade a feature — so those switches are absent, and adding
one would be a specification change rather than a configuration option.

Three settings were **removed** for the opposite reason — documented but wired to nothing:
`SSE_PING_SECONDS`, `EVAL_MAX_CONCURRENCY`, `EVAL_BACKEND`.

## 7. Provisional values

About 62% of settable fields carry a `# TBD(§8.4)` marker: a value chosen on reasoning and not yet
settled by measurement. They cluster in latency, quality and cost tuning — chunking, retrieval,
rerank, the router, the worker, OCR, figure extraction — and are **entirely absent from connection
identity, auth and session security**. Nothing marked provisional is a correctness boundary.

Some have since been closed by measurement and say so at the field, which is the model for how one
should die: `RERANK_SCORE_SCALE` (measured across 90 passages and two models — the models return
only multiples of ten, so a finer scale advertises precision that does not exist),
`OPENAI_JUDGE_MODEL`, and `GRAPH_MAX_RETRIES`.

## 8. Where to look next

- Every variable, with a comment each — [`backend/.env.example`](../backend/.env.example)
- Stack variables for the containerized deployment —
  [`deployment/.env.prod.example`](../deployment/.env.prod.example) and
  [DEPLOYMENT.md §3](DEPLOYMENT.md)
- Getting a machine running — [DEVELOPMENT.md](DEVELOPMENT.md)
