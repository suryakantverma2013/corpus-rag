# Testing

How to test Corpus by hand, what "expected" means, and what to do with what you find.

**Read [LIMITATIONS.md](LIMITATIONS.md) first.** About forty behaviours in this system look like
defects and are decisions. Two minutes there saves an afternoon of adjudicating them.

---

## 1. Before you start

The stack should be running and green:

```bash
docker compose -f deployment/docker-compose.prod.yml --env-file deployment/.env.prod up -d --wait
curl -s localhost:8088/health/ready         # database, broker, object storage
curl -s localhost:8088/health/ready/worker  # + worker heartbeat, malware scanner, OCR
```

Then **http://localhost:8088**, signing in as the administrator you configured.

Four things about a fresh deployment, so none of them reads as a fault:

- **It starts empty.** No documents, no chats. Upload something before expecting an answer.
- **OCR is off.** The worker probe says so explicitly (`"not probed: PARSER_OCR_ENABLED=false"`).
  A scanned PDF will fail until you turn it on — see §3.7.
- **Cloud import needs Google credentials** in the realm and a linked account per user.
- **The first answer costs money.** Real models, real tokens.

## 2. What is already automated — do not test this by hand

The suites below are thorough, and re-testing them manually finds nothing. Counts are from a real
run on 2026-08-22; verify by running rather than trusting this table. They are the one set of
numbers here that nothing asserts — computing them means collecting the suite inside the suite,
and how many *run* depends on what is up — so treat them as a scale, not a checksum.

| Suite | Size | Run |
|---|---|---|
| Backend | 2,292 collected | `cd backend && OCR_LIVE_TEST=1 uv run pytest` |
| — production scenarios | 31 | `uv run pytest tests/scenarios` |
| — route security | 313 | `uv run pytest tests/security` |
| — acceptance guards | 13 | `uv run pytest tests/acceptance` |
| — documentation guards | 29 | `uv run pytest tests/docs` |
| Frontend unit | 1,209 across 58 files (as of 2026-08-29) | `cd frontend && npm test` |
| Browser journey | 1, ~20 s | `cd frontend && CORPUS_PASSWORD='…' npm run e2e` |
| Visual fidelity (headed, both themes) | exit code = failures | `npm run fidelity` |
| Accessibility (axe, 10 surfaces × 2 themes) | exit code = failures | `npm run a11y` |
| Acceptance report | — | `cd backend && uv run python -m tools.acceptance` |

**Skipping is not passing, and how many skip depends on what is running.** The collected total is
stable; the number that actually executes is not. Measured on one machine in one evening: **0
skipped** with every dependency up, **4** with MinIO down, **8** without `OCR_LIVE_TEST=1` and the
OCR sidecar, **25** with the development Keycloak down (plus 3 failures — see below). A backend run
is only meaningful at **0 skipped**, so check the tail of the output, not just the word "passed".

The dependencies a full run needs: PostgreSQL, Redis, MinIO on `:9100`, ClamAV, the OCR sidecar,
and the development Keycloak on `:8081`.

```bash
MINIO_API_PORT=9100 MINIO_CONSOLE_PORT=9101 docker compose -f deployment/docker-compose.yml \
  --profile minio --profile clamav --profile ocr up -d
docker exec deployment-clamav-1 clamdscan --ping 1   # PONG once its signatures have loaded (minutes)
```

**Do not add `--wait` to that command.** The development ClamAV reports *unhealthy* forever — its
stock healthcheck pings `localhost`, which resolves to `::1`, while clamd binds `0.0.0.0`. The
container works fine; only the probe is wrong, and it was fixed for the production stack but not
this one. `--wait` will hang until it gives up. Ping clamd directly instead, as above.

**The backend suite does not talk to the stack you are testing.** It points at the *development*
Keycloak on `localhost:8081`; the production stack runs its own at `localhost:8088/auth` (admin
console on `:8180`). They are different instances with different data. If the development one is not
running you will see **three failures and about twenty-five skips**, and the three failures are
`503`s — which is exactly what that status means here: the identity provider is unreachable. That is
an environment state, not a regression.

The development Keycloak is **not** part of any compose file here — it is a standalone container (or
a native process, depending on the machine), so bringing the stack "all up" with compose will not
start it:

```bash
docker start corpus-keycloak     # the standalone container, if that is how this box runs it
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/realms/corpus   # 200 when it is ready
```

**Specifically out of manual scope**, because it is enumerated and driven with anti-vacuity
controls:

- **Authorization.** Every route × every principal, including anonymous, deleted, deactivated,
  malformed-token and wrong-role.
- **Ownership.** The "not found, never forbidden" asymmetry, including the administrator cases.
- **Rate-limit buckets** — which routes share a budget, per-user separation, and that a path
  parameter does not multiply an allowance.
- **The twelve production scenarios** — duplicate upload, edit-and-re-upload, delete during
  ingestion, query during deletion, worker crash after insert, embedding failure, model change,
  access loss, poisoned document, uncitable answer, retrieval down, parse failure. All driven
  route → worker → route.
- **Copy strings, colour tokens and geometry** — pinned character-for-character and pixel-for-pixel.

## 3. Scripted cases

Where automation does not reach and "expected" is not obvious. Record results per §4.

### 3.1 Ungrounded question
Ask something your documents cannot support.
**Expect:** the abstention message, no citations, and no attempt at an answer. **Not** a plausible
answer from general knowledge — that would be the bug.

### 3.2 `@`-mention actually scopes retrieval
Upload two documents with different content. Type `@`, choose one, ask a question the *other*
answers.
**Expect:** the answer does not use the unmentioned document. A mention narrows; it never merely
boosts. **This path has no automated end-to-end coverage.**

### 3.3 Knowledge-base actions during a live turn
Ask a question; while it is generating, open the knowledge base and try to upload or delete.
**Expect:** a non-destructive notice that actions are paused, not an error dialog. Then wait —
the pause can lapse mid-turn, which is allowed.

### 3.4 Conversation budget exhaustion
Keep asking in one chat until the composer blocks (about 8,900 tokens of transcript).
**Expect:** a clear message that the conversation is full, naming "new chat" as the way on. The
chat stays readable, renameable and citable. Nothing is lost.

### 3.5 Upload rejections through the GUI
Drag in a file over 50 MB, then an unsupported type (`.exe`, `.png`), then exceed the quota.
**Expect:** 413, 415 and 507 copy rendered in the panel, each naming the limit.
**Note:** the file picker filters by extension, so **drag-and-drop is how you reach these** — and
type detection is by content, so a renamed `.exe` is still rejected.

### 3.6 Retry, Replace and scope
Force a failure (a corrupt PDF), then use **Retry**. Then **Replace** a healthy document with new
bytes. Then upload with scope set to **This chat** and confirm it appears only there.
**Expect:** Retry only offered on failure; Replace keeps the old version answering until the new one
is ready; a chat-scoped document is invisible to other chats.

### 3.7 Scanned PDF, both ways
Upload a scanned PDF with OCR off. Then enable it — **both** the profile and the flag — and retry.
```bash
docker compose -f deployment/docker-compose.prod.yml --env-file deployment/.env.prod \
  --profile ocr up -d --wait
# set PARSER_OCR_ENABLED=true, then:
docker compose ... up -d && docker compose ... restart web
```
**Expect:** first, ingestion fails with "no extractable text". After enabling, Retry succeeds.
**The `restart web` is required** — the edge resolves upstreams once and will return 502 otherwise.

### 3.8 A table keeps its column names
Upload a DOCX or Markdown file with a table long enough to split across chunks. Ask for a value from
a row far down it.
**Expect:** the right value, and a citation whose quoted passage includes the **header row**. Without
that header the number has no column and the answer would be guesswork.

### 3.9 Demotion versus disabling
Demote an administrator in the identity provider while they are signed in. Then disable one.
**Expect:** demotion takes up to five minutes to bite; disabling is immediate. **What the GUI does
during that window is untested anywhere** — worth watching closely.

### 3.10 Google Drive, end to end
Link an account, list files, import one, then unlink.
**Expect:** a provider-rendered consent page, one password prompt during linking (there is no
browser session to resume), the imported file behaving exactly like an uploaded one, and unlinking
leaving already-imported documents in place. **Nothing automated performs a real OAuth consent.**

## 4. Area checklists

Surfaces with **no end-to-end coverage** — the largest genuine gap. Exercise each and note anything
surprising.

- **Theme toggle** — switch, reload, sign out and back in. Does it persist?
- **Mention menu** — keyboard only: `@`, arrow, Enter. Escape closes. Typing further filters.
- **Stats panel** — duration ticking, context meter tracking real usage, session averages across
  several answers, sources-referenced count, hide/expand.
- **Score chips** — do they appear a few seconds after the answer, on reload as well?
- **Markdown in real answers** — tables, code blocks, lists, links from an actual model response.
- **Change password and sign out** — including signing out in one tab with another open.
- **Real drag-and-drop** — the automated journey uses a file input, which is not a drop.
- **Admin user management** — create, list, update, delete. **API-only; there is no GUI.**

## 5. Judgement-only checks

No automation can answer these:

- **Is the answer correct and useful?** Automation proves only that it is *grounded* — that claims
  carry citations — never that it is right.
- **Does the cited passage actually support the sentence it hangs off?** And is the page or row
  number right?
- **Does the copy read well?** Strings are pinned for consistency, which is not comprehensibility.
- **Keyboard traversal end to end**, and screen-reader behaviour when an answer arrives or a
  document changes status.
- **Firefox and Safari.** Everything automated is Chromium-only.
- **Two users at once** — concurrent sessions, one deleting a document another is mid-turn on,
  two tabs sharing a session.

## 6. What to do with a finding

**Ask the triage question first: is this a defect, or a decision I disagree with?**

If [LIMITATIONS.md](LIMITATIONS.md) names the behaviour, it is not a bug. It may still be *wrong* —
but changing it is a design decision, and it belongs in a design discussion rather than a bug list.
Say so explicitly; do not file it and let it be re-argued from scratch.

Otherwise, record it. Each finding needs:

- **what you did** — enough to reproduce, including which stack and which browser
- **what you expected**, and why
- **what happened**
- **how bad it is** — does it lose data, block a task, look wrong, or merely annoy?

**Every fix starts with a failing test that names the defect**, then the fix. That is how every
defect in this project has been closed; it is the only way to know the fix worked and that the
defect stays gone.
