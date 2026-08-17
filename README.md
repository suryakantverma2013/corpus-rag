# Corpus

An enterprise RAG chatbot. Upload your documents, ask questions in a chat, and get answers that
are **grounded**: every claim carries a citation chip that resolves to the passage it came from,
and when the retrieved material does not support an answer the system says so instead of
answering from the model's pre-training.

```
┌──────────┬──────────────────────────────────────────┬──────────────┐
│ Chats    │  You: What is the retention period?      │  Session     │
│          │                                          │  Model       │
│ New chat │  Corpus: Records are kept ninety-seven   │  Context     │
│ ▸ Policy │  days, then purged [policy.pdf p.4].     │  ████░░ 62%  │
│ ▸ Specs  │  ────────────────────────────────────    │  Relevancy   │
│          │  grounded in 1 passage · policy.pdf      │  0.94  ████  │
│ 📚 KB (7)│  👍 👎  ⟳ Regenerate                      │  Faithfulness│
└──────────┴──────────────────────────────────────────┴──────────────┘
```

---

## What it does

- **Grounded answers with inline citations.** Hover a chip to see the quoted passage, its document
  and its locator — page for PDFs, section for DOCX and Markdown, row range for CSV.
- **It abstains.** A groundedness gate runs *before* anything reaches the browser, so an
  unsupported question gets an honest "the documents do not say" rather than a confident guess.
- **Hybrid retrieval.** Dense vectors (pgvector) and lexical search (PostgreSQL FTS) fused by
  Reciprocal Rank Fusion, then reranked — so acronyms, part numbers and error codes are found as
  reliably as paraphrases.
- **Query-adaptive routing.** Vague follow-ups get rewritten, multi-part questions decomposed, and
  questions whose wording does not match the corpus get a hypothetical-document probe.
- **Asynchronous ingestion** for PDF, DOCX, CSV and Markdown, with live per-document status over
  SSE, incremental re-embedding (an edit re-embeds one block, not the document), malware and
  structural screening, and per-user quotas.
- **Per-answer quality scores**, judged after the fact so they never delay a reply.
- **Documents scoped globally or to a single chat**, with `@`-mentions to narrow a question to
  particular files.
- **Enterprise auth** via Keycloak (OIDC), two roles, audited.
- **Accessible and themed** — keyboard-navigable, screen-reader tested, light and dark.

## Run it

```bash
cp deployment/.env.prod.example deployment/.env.prod
# fill in every CHANGE_ME and OPENAI_API_KEY

docker compose -f deployment/docker-compose.prod.yml \
               --env-file deployment/.env.prod \
               up -d --wait --build
```

Open **http://localhost:8088**. That brings up the API, the worker, the SPA and its edge proxy,
PostgreSQL + pgvector, Redis, MinIO, ClamAV and Keycloak, runs the migrations and both bootstrap
steps, and comes up in about a minute. Full runbook: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## Develop it

The application runs natively against a few infrastructure containers:

- [`backend/README.md`](backend/README.md) — layout, running the API and the worker, conventions
- [`frontend/README.md`](frontend/README.md) — design tokens, theming, the generated API client
- [`deployment/README.md`](deployment/README.md) — the development infrastructure stack
- [`deployment/keycloak/README.md`](deployment/keycloak/README.md) — realm bootstrap

Every configuration variable is documented in [`backend/.env.example`](backend/.env.example).

## Architecture in six lines

A React SPA and a FastAPI backend answer on **one origin** behind a reverse proxy. A chat turn
runs as a checkpointed LangGraph state machine — screen → route → retrieve → rerank → generate →
**gate** → finalize — and nothing reaches the browser until the gate passes. Ingestion is an arq
worker: scan → parse → chunk → embed → index. PostgreSQL holds the tables, the vectors, the
full-text indexes and the graph checkpoints; MinIO holds the original files; Keycloak owns
identity. The API and the worker are the same image with different commands.

→ **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

## Documentation

| | |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | design principles, topology, the chat turn, latency budget, retrieval and grounding design, failure semantics, rejected alternatives, known limitations |
| [Data model](docs/DATA_MODEL.md) | schema and ownership, vectors and FTS, chunk identity across versions, retention, rejected alternatives, limitations |
| [Security](docs/SECURITY.md) | threat model, auth and authorization, content controls, prompt injection, how it is verified, limitations |
| [Evaluation](docs/EVALUATION.md) | the three instruments, gate vs judge, measured judge behaviour, reading the numbers, limitations |
| [Deployment](docs/DEPLOYMENT.md) | the production stack, Keycloak URLs, TLS, operations, troubleshooting, and what this deployment is *not* |

## Tests

| Suite | Count | Run |
|---|---|---|
| Backend | **1,790** | `cd backend && uv run pytest` |
| — of which route-level security | 282 | `uv run pytest tests/security` |
| Frontend unit | **1,141** | `cd frontend && npm test` |
| End-to-end (real browser, real stack) | 1 journey, ~20 s | `npm run e2e` |
| Visual fidelity (headed, both themes) | — | `npm run fidelity` |

There is no CI in this repository; the suites are run locally. The end-to-end and fidelity suites
need a running stack and will fail — rather than skip — without one.

## Stack

**Backend** Python 3.14 · FastAPI · LangGraph · SQLAlchemy 2 (async) · PostgreSQL + pgvector ·
Alembic · arq + Redis · MinIO/S3 · ClamAV · structlog · [uv](https://docs.astral.sh/uv/)
**Frontend** React · Vite · TypeScript, with API types generated from the OpenAPI document
**Models** OpenAI — chat, embeddings, reranking and the evaluation judge
**Identity** Keycloak (OIDC)

## License

GPL-3.0. See [LICENSE](LICENSE).
