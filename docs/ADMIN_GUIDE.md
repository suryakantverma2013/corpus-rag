# Corpus — administrator guide

For the person who runs a Corpus deployment: accounts, the corpus itself, the models it calls, and
the records it keeps. None of it is about *using* the chat — that is [USER_GUIDE.md](USER_GUIDE.md).

Corpus has **two roles and no more** (R-64): *user* and *administrator*. Everything below needs the
second.

- [Where identity lives](#where-identity-lives)
- [Creating and removing users](#creating-and-removing-users)
- [Taking access away, and how fast it happens](#taking-access-away-and-how-fast-it-happens)
- [The knowledge base across users](#the-knowledge-base-across-users)
- [Re-embedding, and when you are forced to](#re-embedding-and-when-you-are-forced-to)
- [Changing a model without a deploy](#changing-a-model-without-a-deploy)
- [The audit trail](#the-audit-trail)
- [Rate limits](#rate-limits)
- [What is kept, and for how long](#what-is-kept-and-for-how-long)

---

## Where identity lives

**Keycloak owns credentials and roles; Corpus owns everything else.** There is no password hash and
no role column in the Corpus database — `users` is a mirror keyed on the Keycloak subject, holding
the email, the display name and whether the account is active.

Two practical consequences:

- A password is changed in Corpus or in Keycloak, and either works. Corpus's own change-password
  screen calls Keycloak.
- **A role is a claim inside the access token.** That is what makes the timing below matter.

> ### Read this before you touch the realm
>
> Corpus authenticates by **backend-mediated ROPC** — the browser never talks to Keycloak during
> login, so **Keycloak cannot ask a user for anything**. Any *required action* is therefore
> impossible to satisfy, and a user who carries one is **locked out permanently**: the token
> endpoint answers `400 invalid_grant` / *"Account is not fully set up"*, and Corpus has no recovery
> surface.
>
> So: **do not enable `UPDATE_PASSWORD`, `VERIFY_EMAIL`, `CONFIGURE_TOTP` or
> `TERMS_AND_CONDITIONS`** as a realm default, and do not set one on an individual user, unless a
> browser login flow ships first. A password *rotation* policy is the same trap wearing a
> security-improvement hat.
>
> This is not hypothetical — it is a defect that reached this deployment once. The shipped realm
> makes `firstName`/`lastName` optional for the same reason.

---

## Creating and removing users

```
POST   /api/v1/users            create
GET    /api/v1/users            list
PATCH  /api/v1/users/{id}       change display name, role or active state
DELETE /api/v1/users/{id}       remove
```

Creating a user takes an email, a password and optionally a display name and a role. **Supply the
display name.** The schema calls it optional and the realm does not: a user created without one can
be created and then never sign in.

There is no self-service registration, by design.

**You cannot demote, disable or delete yourself.** The API refuses with a conflict rather than
letting an administrator remove their own access — the guard is per-action and deliberate. Two
administrators deleting *each other* concurrently can still both succeed; recover through the
Keycloak console.

---

## Taking access away, and how fast it happens

This is the section worth reading twice, because Corpus revokes two things at two speeds.

| What you change | When it takes effect |
|---|---|
| **Disabling an account** | The user's **very next request**, even with a valid token in hand |
| **Demoting an administrator** | When their **access token expires** — 300 s on the shipped realm — or sooner if they refresh |

The reason is the split above: account state is read from the database on every request, while the
role is read from the token the caller presents. Access control *is* enforced on every request —
nothing is cached — but the role **data** is as old as the token.

> **When someone must lose administrator access now, disable the account.** Demoting alone leaves a
> window of up to the token lifespan. Re-enable afterwards if they should remain an ordinary user.

None of this is a defect; it is the price of having one source of truth for identity and no
introspection round trip on every request. It is written down because a reader of *"enforced
server-side on every request"* is entitled to conclude otherwise.

---

## The knowledge base across users

Documents belong to the user who uploaded them, and a user's questions search only their own
documents — *global* ones plus anything attached to the conversation they are in. There is no
shared corpus and no cross-user visibility.

As an administrator you can **read** any document's metadata and job state, and you can drive a
re-embed. You cannot read another user's conversations: every conversation and message route
requires you to be the owner, and answers `404` for an administrator exactly as it would for a
stranger. That is deliberate — a turn run against someone else's chat would write into their
transcript.

A document that will not process shows **Failed** with a reason. `GET /api/v1/jobs/{id}` gives the
detail: the error code, which attempt it is on, and which document version was being built.

A document may instead show **Ready** with *text may be unreadable* beside it, and that one is worth
recognising because nothing is broken. The PDF's text layer extracted characters that are probably
not what the page displays; the document is indexed, searched and answerable, and questions needing
the affected pages will simply tend to abstain. **OCR will not rescue it** — extracted characters
always beat recognised ones, so a bad text layer stays bad. The fix is a better copy of the file.
The measurement is advisory and nothing gates on it, which is also why its threshold is not exposed
in `x-corpus-env`: tuning it is a corpus measurement rather than an operator action, exactly as with
the figure-detection floors ([CONFIGURATION.md §1](CONFIGURATION.md)).

---

## Re-embedding, and when you are forced to

Corpus stamps every chunk with the pipeline that produced it: the **embedding model**, the
**chunking version** (which folds in the `CHUNKER_*` knobs) and the **preprocessing version**.
Change any of those and existing chunks are stale — they still answer, from the old vector space,
and nothing fails.

**Three things force it**, and all three are configuration you control:

1. Changing `OPENAI_EMBEDDING_MODEL`, or the embedding model slot.
2. Retuning any `CHUNKER_*` value.
3. A `PREPROCESSING_VERSION` bump — which ships when parsing itself changes, as it did when OCR and
   table extraction landed.

Find out where you stand:

```bash
cd backend
uv run python -m tools.reembed plan            # read-only: what is stale, and what it would cost
uv run python -m tools.reembed run --limit 10  # queue ten rebuilds
```

`plan` writes nothing. `run` queues rebuilds and returns immediately — the **arq worker** does the
work, so it must be running or nothing progresses. Staleness is read from each chunk's stored
provenance, not recomputed, so the report names which input drifted.

**It is safe against a live deployment and it is not free.** A rebuild copies the stored original
forward to a new version and re-embeds the whole document; the version already indexed keeps
answering until the new one is ready. But it spends embedding calls on every chunk, so work through
a large corpus in bounded batches rather than all at once.

The same operation is available per document over HTTP:

```
GET  /api/v1/admin/documents/stale
POST /api/v1/admin/documents/{id}/reembed
```

A document that is **not** stale is refused with a conflict rather than rebuilt — the point of the
tool is to re-drive what the configuration has invalidated, not to re-run embeddings on demand.

---

## Changing a model without a deploy

Six call sites can be pointed at a different model at runtime:

```bash
uv run python -m tools.set_model show
uv run python -m tools.set_model set chat gpt-4o
uv run python -m tools.set_model clear chat
```

Slots: `chat`, `router`, `rerank`, `judge`, `judge_escalation`, `embedding`. An unset slot uses its
`OPENAI_*` environment default, and `clear` returns it there. There is deliberately no way to "set" a
slot back to the default by typing the same id — that would leave a row pinning a value the
deployment could no longer move by redeploying.

**The embedding slot is not like the other five.** `OPENAI_EMBEDDING_MODEL` is one of the fingerprint
inputs above, so moving it leaves every existing chunk in the old vector space until it is rebuilt.
`set embedding` therefore prices the flip first, needs `--yes`, and refuses to skip its verification
— it asks the provider how many dimensions come back, which no offline check can answer. Plan a
re-embed at the same time.

---

## The audit trail

```
GET /api/v1/audit?event_type=...&actor_id=...&limit=...&offset=...
```

Six categories are recorded: `AUTH`, `USER_ROLE_CHANGE`, `DOCUMENT_UPLOAD`, `DOCUMENT_REPLACE`,
`DOCUMENT_DELETE`, `PERMISSION_CHANGE`.

The trail is **append-only and kept indefinitely.** No pruning ships, and that is a decision rather
than an omission: the value of an audit record is being complete, and a retention horizon chosen
from no obligation destroys exactly that. If a retention obligation ever applies to you, it needs
building — `prune_turn_telemetry` is the working mirror to copy.

It records *actions*, not content. No question, no answer and no document text ever enters it.

---

## Rate limits

Per principal, enforced at the edge of the API:

| Setting | Default | Covers |
|---|---|---|
| `RATELIMIT_LOGIN` | `10/minute` | sign-in attempts |
| `RATELIMIT_REFRESH` | `30/minute` | token renewal |
| `RATELIMIT_CHANGE_PASSWORD` | `5/minute` | password changes |
| `RATELIMIT_CHAT` | `20/minute` | asking and regenerating — **one shared budget**, because a regenerate costs what a question costs |
| `RATELIMIT_UPLOAD` | `20/minute` | upload, replace, retry and cloud import together |

`RATELIMIT_ENABLED` turns the whole mechanism off; `RATELIMIT_STORAGE_URI` points it at Redis.

Two properties worth knowing. The limiter **fails open** — if Redis is unreachable, requests are
allowed rather than refused, because a rate limiter is a spend control and not a security boundary.
And the chat and upload budgets are **shared across the routes in their group**, so a caller cannot
multiply their allowance by spreading work over sibling endpoints.

---

## What is kept, and for how long

| Data | Retention |
|---|---|
| Conversations and messages | Until the user deletes them |
| Documents and their chunks | Until deleted; a delete removes the vectors outright |
| Audit trail | **Indefinitely**, append-only |
| Per-turn telemetry | `TELEMETRY_RETENTION_DAYS`, default 90; **`0` means keep forever** |
| LangGraph checkpoints | Newest few per conversation, pruned on a schedule |
| Evaluation scores | For the life of the message they belong to — they are a column on it |

Note the polarity trap: `0` means *keep forever* for telemetry, because the value is read as a
horizon. It is the opposite of the checkpoint sweep's interval, where `0` disables the job.

---

## Where to go next

- Installing, upgrading, backing up, and what breaks when a setting is wrong →
  [DEPLOYMENT.md](DEPLOYMENT.md) and [CONFIGURATION.md](CONFIGURATION.md)
- The security posture and its accepted exceptions → [SECURITY.md](SECURITY.md)
- What Corpus deliberately does not do → [LIMITATIONS.md](LIMITATIONS.md)
