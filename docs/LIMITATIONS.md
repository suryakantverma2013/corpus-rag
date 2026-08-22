# Known limitations

**Read this before testing.** Corpus has a lot of behaviour that looks wrong and is deliberate.
This page collects it in one place so a real defect is not buried under ninety arguments that were
already had.

Every entry is one line, a reason, and a pointer to wherever it is properly argued. Nothing here is
copied from those places — follow the pointer for the full case.

**★ marks the entries most often mistaken for defects.** There are about forty. Skimming just those
takes two minutes and is the highest-value thing you can do before starting.

**If you disagree with one of these, that is worth saying — but it is a design argument, not a bug
report.** See [TESTING.md](TESTING.md#6-what-to-do-with-a-finding).

---

## 1. Answering and chat

★ **The system refuses rather than guesses.** Ask something your documents do not support and you
get *"I couldn't ground an answer to that in your documents…"* — never a best-effort answer from the
model's own training. There is no "try anyway" mode and no setting to add one.
→ [ARCHITECTURE.md §"There is no best effort mode"](ARCHITECTURE.md)

★ **You never see the answer it refused to serve.** When the groundedness gate rejects a draft, the
refusal *replaces* it. Serving the text under an "abstained" label would be ungrounded prose with
no citations wearing an honest-looking badge.

★ **"Found nothing" and "everything I found was just deleted" read identically.** Both are the same
fact from your seat, and the second would explain an internal race you cannot act on.

★ **The refusal names no score and no threshold.** The number moves with a tuning knob and would
invite arguing with the machine rather than rephrasing.

★ **A blocked prompt-injection attempt returns its own copy with no error code**, names no rule and
echoes nothing of what you typed — deliberately, so attempts cannot be used to probe the filter.
→ [SECURITY.md §5](SECURITY.md)

★ **Prompt-injection screening is evadable and is meant to be.** Pattern rules are a tripwire; the
real control is structural — one instruction channel, fenced context, no tools. Getting a payload
past the screen is expected. Getting it to *change what the system does* is the bug.
→ [SECURITY.md §5, §11.3](SECURITY.md)

★ **Screening never blocks your documents' text, only your query.** Otherwise one uploaded security
policy containing "ignore previous instructions" would make every question that retrieves it
permanently unanswerable.

★ **A previous answer re-enters the next turn as trusted text**, outside the injection fence. Known
and accepted, bounded by that answer having passed the gate itself. → [SECURITY.md §11.4](SECURITY.md)

★ **Regenerate has no undo, and can make things worse.** It replaces the answer, its citations and
its scores unconditionally — including when the re-run abstains or is injection-blocked. The confirm
dialog is the only guard.

★ **A failed turn is shown but never saved.** Reload and the error message is gone. Storing error
copy would charge it against the conversation's token budget, so a run of provider failures would
eat the chat.

★ **A conversation that hits its token budget is frozen, permanently.** The composer blocks at about
8,900 tokens of transcript; there is no summarisation or rolling window, and starting a new chat is
the only way on. Everything else about the chat keeps working.

**History is sent to the model untruncated** — the budget above is what prevents overrun, so there
is no windowing to observe.

**Failures collapse into five classes** (`LLM_ERROR`, `RETRIEVAL_UNAVAILABLE`, `RATE_LIMITED`,
`TIMEOUT`, `SYSTEM_FAILURE`) — a class exists only where it changes what you should do next.

**There is no switch to disable grounding checks**, not even for testing. `GATE_ENABLED` and
`TELEMETRY_ENABLED` deliberately do not exist, because their off state would remove a guarantee
rather than degrade a feature.

## 2. Documents and ingestion

★ **OCR ships off.** A scanned PDF therefore fails ingestion outright with `NO_EXTRACTABLE_TEXT`.
Turning it on needs **two** switches that must agree — the `ocr` compose profile *and*
`PARSER_OCR_ENABLED=true` — and neither half complains on its own.
→ [DEPLOYMENT.md §10](DEPLOYMENT.md)

★ **A table on a scanned page comes back as reading-order text, not a grid.** Table extraction is
layout analysis over a *text layer*, which a scanned page does not have. This is the case most
people test first. → [SECURITY.md §11.11](SECURITY.md)

★ **A table with no ruling lines is not detected at all** and ingests as ordinary text. Detecting
whitespace-aligned tables would also "detect" any column-ish prose layout, and rewriting a page of
prose as a grid is worse than missing a table. There is deliberately no setting to change this.

★ **A poor built-in text layer is never improved by OCR.** If a page yields any characters at all,
those characters win — a garbled PDF stays garbled. → [SECURITY.md §11.12](SECURITY.md)

★ **A figure inside a text page may silently not be searchable** while the document still ingests
fine. Recognition fails *open* per page; only a document with no text anywhere fails closed.

★ **Limits reject, they never truncate.** An over-long or over-complex document fails with
`CONTENT_LIMIT_EXCEEDED` rather than ingesting partially.

★ **The document meta line shows page counts for PDFs only.** DOCX, Markdown and CSV fall back to a
chunk count, because inventing a page number would produce a citation you cannot check.

★ **Replacing a document destroys the previous version.** There is no chunk-level history and no
rollback, and old citation ids stop resolving. Answers keep their quoted text, which is why this is
safe. → [DATA_MODEL.md §5, §9.5](DATA_MODEL.md)

★ **A replace briefly blocks a delete on the same document** while the new bytes are stored. The
lock is held across the upload deliberately.

★ **Quota enforcement is best-effort.** Parallel uploads can push you past the 10 GB allowance.
→ [SECURITY.md §11.7](SECURITY.md)

★ **Uploading the same file twice returns "already in your knowledge base"**, not a second document.
Deduplication is by content checksum, so a renamed copy is still a duplicate.

★ **Documents stuck at "Queued" usually mean the malware scanner is unhealthy.** The worker gates on
ClamAV; the API deliberately does not, so chat keeps working while ingestion stops.

★ **"Add from cloud drive" with no linked account is a full-page redirect to the sign-in
provider** — not an inert button and not an error. Not being linked is the normal state.

★ **Unlinking Google Drive does not remove documents you already imported.** Imports are copies;
unlinking revokes future imports only.

**Cloud linking shows a provider-rendered page**, unlike the rest of the product. It happens once,
only for users who want Drive, and never on the login path.

**Only Google Drive ships**, though the mechanism is provider-agnostic — a formally recorded
accepted exception. → [ACCEPTANCE.md §3](ACCEPTANCE.md)

**Small or decorative images are skipped by OCR** without comment, and a very long scan can hit a
page ceiling or per-page timeout.

**A long healthy ingestion can show a "stalled" hint.** It is derived from a timeout threshold, not
from a failure.

**Text is not de-hyphenated, case-folded, stop-word stripped or NFKC-normalised** — each of those
destroys information that cannot be recovered afterwards.

**A DOCX table's header row is honoured only if the file declares one.** An unmarked first row is
treated as data.

## 3. Search and answer quality

★ **The keyword half of search is not BM25.** It is PostgreSQL full-text ranking, so a common word
counts as much as a rare one and long passages are not penalised. A recorded deviation with a
revisit trigger. → [ARCHITECTURE.md](ARCHITECTURE.md), [ACCEPTANCE.md §3](ACCEPTANCE.md)

★ **The reranker is a language model scoring passages, not a cross-encoder.** Same purpose,
different mechanism, recorded rather than hidden.

★ **When reranking fails you see no score at all** on citation cards — the score clause disappears
rather than falling back to another number, because the fallback number means something different
and is comparable only within a single turn.

★ **Rerank scores come in tenths.** Expect ties and no meaningful second decimal — measured across
ninety passages and two models, the model simply does not produce finer judgements.

★ **A passage can argue for its own relevance** and move up the ranking. Bounded: it can only
reorder documents already inside your own scope.

**Low groundedness does not trigger a retry** — measured, a second pass over the same passages was
no better and cost several seconds.

**The same text can appear twice** in retrieved context, because chunks overlap and overlapping
neighbours are not deduplicated.

**Groundedness is measured structurally, not semantically** — whether claims carry citations, not
whether the cited passage means what the sentence says. The post-hoc judge covers the rest.

## 4. Interface and accessibility

★ **Twelve of twenty-eight colour pairs fail WCAG AA in dark theme, eighteen in light.** This is an
enumerated, accepted exception, not a backlog — the palette is fixed by the design handoff. Two
rules hold instead: **colour is never the only way information is conveyed**, and **no new pair may
drop below threshold**. A new failing pair *is* a bug. → [ACCESSIBILITY.md](../frontend/ACCESSIBILITY.md)

★ **Every destructive confirm button** (delete chat, delete document, unlink) renders its label at
3.06:1 in light theme. Live instance of the accepted group above.

★ **There is no download, export, preview or "open document" anywhere.** Clicking a citation shows
the quoted passage; it cannot open the file. Corpus never serves an uploaded byte back, which is
what keeps signature scanning defence-in-depth rather than the only line.
→ [SECURITY.md §11.6](SECURITY.md)

★ **The chat list is unbounded — no paging, no "load more".** Every conversation is returned and
rendered. Deliberate: paging the most-used surface to solve a problem nobody has hit is the wrong
trade. → [ACCEPTANCE.md §4](ACCEPTANCE.md)

★ **The login screen has no password reset and no sign-up.** Accounts are administrator-created and
there is no self-service reset.

★ **The mention menu is not in the tab order.** You enter it by arrowing down from the composer,
deliberately, so keyboard users are not forced through every document to leave the field.

**Only a trailing `@` opens the mention menu** — an `@` earlier in the line is a mention you already
finished.

**Score chips show two decimals** even though the judge is not that precise. The numeral is what
carries the information where colour cannot, and the tooltip says it is indicative.

**Answers appear all at once rather than streaming** if a proxy re-enables response buffering.

**Screen-reader landmark warnings on two elements are accepted** — the visually-hidden page heading
and the citation card, both positioned deliberately.

## 5. Accounts and access

★ **Demoting an administrator does not take effect immediately** — the role lives in their token
and persists until it expires, up to about five minutes. **Disabling an account is immediate.** To
remove access now, disable; do not merely demote. → [SECURITY.md §4, §11.2](SECURITY.md)

★ **Anything that is not yours returns "not found", never "forbidden"** — including for
administrators. Distinguishing the two would turn every id in a URL into an existence oracle.

★ **Login can fail with "Account is not fully set up"** if any required action is set on the account
in the identity provider. Never set a temporary password.

★ **The login response contains no refresh token.** It is an httpOnly cookie, unreadable by script.

★ **Sessions are lost on every reload at a non-localhost plain-HTTP origin**, because the session
cookie requires a trustworthy origin. Terminate TLS at your edge.

**There are two roles only**, administrator and user. Delegated administration belongs in the
identity provider, not here.

**Isolation is per user, not per tenant.** A tenant column exists but backs nothing.
→ [SECURITY.md §11.1](SECURITY.md)

**Re-embedding is administrator-only even for the document's owner**, because it spends the
deployment's budget rather than the caller's.

## 6. Administration and operations

★ **The audit trail is never pruned and grows forever.** Deliberate — an audit trail's value is
being complete, and no retention obligation has been stated. → [SECURITY.md §11.5](SECURITY.md)

★ **Telemetry rows survive conversation deletion.** A user deleting their chat must not be able to
rewrite an operator's history. A test that counts the whole telemetry table will therefore fail
after any real use.

★ **Two retention settings use `0` to mean opposite things** — one disables a sweep, the other means
"keep forever". One is a period, the other a horizon. → [DATA_MODEL.md §6](DATA_MODEL.md)

★ **Disk usage does not drop after pruning.** The deliverable is bounded growth; space returns when
the database vacuums.

★ **Nothing is encrypted at rest by the application** — a formally accepted exception, and an
operator responsibility the deployment documents rather than provides. Backups are the part most
often missed — and DEPLOYMENT.md §9.2 now produces one to encrypt. → [DEPLOYMENT.md §8](DEPLOYMENT.md),
[SECURITY.md §11.13](SECURITY.md)

★ **The rate limiter fails open.** If Redis blips, limits stop applying rather than locking
everyone out.

★ **There is no CI.** No suite runs automatically on a change. → [SECURITY.md §11.9](SECURITY.md)

★ **Secrets are environment variables in a file**; rotation is a redeploy.

★ **Everything returns 502 after changing any container environment value** until the edge is
restarted — the proxy resolves its upstream once, at start. → [DEPLOYMENT.md §12](DEPLOYMENT.md)

★ **Changing the embedding model needs a migration and a full re-embed**, and nothing re-embeds a
healthy corpus automatically. It is a cost event, run deliberately.

★ **Judge scores are indicative, not exact.** Two frontier judges disagree by 0.25 or more on about
22% of scores. → [EVALUATION.md](EVALUATION.md)

★ **Only two of four evaluation metrics run on live turns.** The other two need a reference answer,
which a real chat turn does not have.

★ **Thumbs up/down is recorded and never acted on.** No tuning loop — these thresholds are safety
controls, and a feedback loop would let users switch off grounding by disliking answers.

**The database does not enforce lifecycle values** — a direct SQL write can insert an invalid state.
An accepted cost of avoiding a migration per enum change.

**The reference deployment is single-node**: no TLS, no orchestrator, no autoscaling,
zero-downtime not supported. Backup and restore are a documented, verified procedure with a
script, but nothing runs it on a schedule and nothing copies the result off the host.
→ [DEPLOYMENT.md §14](DEPLOYMENT.md), [§9.2](DEPLOYMENT.md)

**A new OCR language pack is ignored** unless its volume is removed first — it seeds only while
empty.

**Evaluation scores have no retention** and are deleted with their message, so a score never
outlives the answer beside it.

**Model-override changes emit a log line, not an audit row.**

## 7. Speed

★ **Five to six seconds pass before the first word appears.** Nothing streams until the whole answer
has been generated *and* checked, because you cannot retract an ungrounded sentence someone has
already read. The check itself takes microseconds; the time is retrieval, reranking and generation.
→ [ARCHITECTURE.md §4.1](ARCHITECTURE.md)

★ **Score chips arrive after the answer**, not with it — evaluation runs afterwards, off your clock.

★ **The document list updates by polling**, so a status change can lag by a second or two.

★ **The "actions paused" lock can expire mid-turn**, re-enabling document actions while an answer is
still generating. The lock is a courtesy, not a correctness mechanism.

**OCR adds roughly two seconds per recognised page**, single-threaded by design.

**Uploads over 50 MB are rejected before storage**, and the whole stack times out on first start if
Docker has under about 6 GB.

---

## Deliberately absent

Things testers look for and do not find. None of these is missing by oversight:

- **Any way to download, export, preview or open a source document.**
- **Password reset and self-service sign-up.**
- **Agent tool-calling.** The model retrieves and writes; it never acts.
- **A GUI for user administration.** Creating and managing users is API-only.
- **Voice input, an analytics dashboard, model switching in the UI, automatic chat titles, and
  graph-based retrieval** — all explicitly out of scope.
- **Switches that would turn requirements off**: `GATE_ENABLED`, `CONTEXT_ENABLED`,
  `TELEMETRY_ENABLED`, `PARSER_TABLE_ENABLED`, `OCR_BACKEND`.
- **CORS middleware.** Production is same-origin by design.

---

## When this page is wrong

It is a snapshot, written by hand, with no automated guard yet. If you find an entry that no longer
matches what the software does, that is a defect **in this document** — file it like any other
finding. It is worth more than most product bugs, because everything downstream trusts this page.
