# Corpus documentation

Thirteen documents, and which one you want depends on why you are here.

## Start here

| You are | Read |
|---|---|
| **Using Corpus** — asking questions, adding documents, reading citations | [USER_GUIDE.md](USER_GUIDE.md) |
| **Running it for other people** — accounts, re-embedding, the audit trail | [ADMIN_GUIDE.md](ADMIN_GUIDE.md) |
| **Installing or upgrading it** | [DEPLOYMENT.md](DEPLOYMENT.md), then [CONFIGURATION.md](CONFIGURATION.md) |
| **Working on the code** | [DEVELOPMENT.md](DEVELOPMENT.md), then [ARCHITECTURE.md](ARCHITECTURE.md) |
| **Deciding whether it does what you need** | [ARCHITECTURE.md](ARCHITECTURE.md) and [LIMITATIONS.md](LIMITATIONS.md) |
| **Surprised by something** | [LIMITATIONS.md](LIMITATIONS.md) — most surprises here are decisions |

## Everything, by subject

### Using it

- **[USER_GUIDE.md](USER_GUIDE.md)** — every task in the interface, with screenshots: signing in,
  asking, reading a citation, uploading, `@`-mentions, cloud import, the session panel, themes.
- **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** — user management, revocation and its two speeds,
  re-embedding, runtime model slots, the audit trail, rate limits, retention.

### Running it

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — the production stack, Keycloak's two URLs, TLS, backup and
  restore, troubleshooting, and what this deployment deliberately is *not*.
- **[CONFIGURATION.md](CONFIGURATION.md)** — what you actually set, the settings that refuse to
  boot when they disagree, coupled knobs, and the three surfaces a value must cross to reach a
  running container.
- **[SECURITY.md](SECURITY.md)** — threat model, authentication and authorization, content
  controls, prompt injection, browser content restrictions, and the exceptions that are accepted
  rather than fixed.

### Building on it

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — design principles, topology, the anatomy of a chat turn,
  the latency budget, retrieval and grounding, failure semantics, and rejected alternatives.
- **[MODULE_MAP.md](MODULE_MAP.md)** — what each package owns, what it must **not** do, and the
  test that fails when the rule erodes.
- **[DATA_MODEL.md](DATA_MODEL.md)** — the schema, vectors and full-text search, chunk identity
  across document versions, and retention.
- **[HTTP_API.md](HTTP_API.md)** — every endpoint, parameter, status and schema. Generated from
  `backend/openapi.json` and guarded byte-for-byte against it.

### Checking it

- **[TESTING.md](TESTING.md)** — how to test by hand, what is already automated, and what to do
  with a finding.
- **[EVALUATION.md](EVALUATION.md)** — the three instruments, the gate versus the judge, measured
  judge behaviour, and how much to trust a score.
- **[ACCEPTANCE.md](ACCEPTANCE.md)** — every acceptance-critical value, the evidence that covers
  it, and the requirements dispositioned rather than met.

### Knowing where the edges are

- **[LIMITATIONS.md](LIMITATIONS.md)** — the behaviour that looks like a defect and is a decision.
  Worth reading before filing one.
- **[GLOSSARY.md](GLOSSARY.md)** — the words this documentation uses in a particular way.

## Two conventions

**Section references name their document.** A bare `§7` means *this* document's section 7; anything
else is written `DEPLOYMENT.md §9.2`. A number belonging to the private specification is written
`spec §8.80` and cannot be followed — that document does not ship.

**Numbers here are checked where they can be.** Suite sizes, migration counts, package sizes,
settings counts and the acceptance row count are all compared against the thing they count by
`backend/tests/docs/`, so a stale figure fails a test rather than misleading a reader. Where a
number *cannot* be computed from here it carries the date it was true.
