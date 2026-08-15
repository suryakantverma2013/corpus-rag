# End-to-end happy paths (T-603)

`npm run e2e` drives a real browser through the whole product: **login → upload → ask → cite →
feedback → regenerate → rename → delete.**

This is the only thing in the repository that crosses every seam at once. `vitest` (1,141 tests)
runs against jsdom with `fetch` mocked; the backend suite (1,728) drives ASGI inside a
rolled-back transaction with fake embeddings and a stub queue. Both are right, and neither can
tell you that an upload actually finishes ingesting or that an answer actually reaches a bubble.

## What has to be running

The split is deliberate: **the application runs natively on the laptop; only its infrastructure
is containerised.**

| Component | How it runs | Port |
|---|---|---|
| Postgres (+pgvector) | **native** | 5432 |
| Keycloak | **native** | 8081 |
| Redis | docker (`deployment-redis-1`) | 6379 |
| MinIO | docker (`deployment-minio-1`) | 9100 |
| ClamAV | docker (`deployment-clamav-1`) | 3310 |
| Backend, arq worker, Vite | **native processes** | 8000 / — / 5173 |

```bash
# infrastructure (from the repo root) — these three only
MINIO_API_PORT=9100 MINIO_CONSOLE_PORT=9101 \
  docker compose -f deployment/docker-compose.yml --profile minio --profile clamav up -d
docker exec deployment-clamav-1 clamdscan --ping 1     # expect PONG; its healthcheck lies

# the application, natively — three terminals
cd backend && uv run uvicorn app.main:app --loop app.runtime:selector_loop
cd backend && uv run arq workers.main.WorkerSettings
cd frontend && npm run dev -- --port 5173 --strictPort

# then
cd frontend && CORPUS_PASSWORD='<KEYCLOAK_LIVE_ADMIN_PASSWORD from backend/.env>' npm run e2e
```

Four things that will each cost you half an hour if you skip them:

- **`--loop app.runtime:selector_loop` is not optional on Windows.** psycopg's async driver needs
  `loop.add_reader`, which the default `ProactorEventLoop` lacks. Without it the app boots
  perfectly and the *first chat turn* raises `CheckpointerConfigError`. (`--reload` also works,
  because it runs uvicorn in a subprocess that picks a selector loop.)
- **Without the arq worker an upload is accepted and never leaves `Queued`.** `globalSetup`
  probes `/health/ready/worker` precisely so that shows up as one line rather than as a
  two-minute wait on a document that never goes `Ready`.
- **Drive the Vite origin, never `:8000`.** The backend ships no CORS middleware by design;
  `vite.config.ts` proxies `/api` and `/health` to it, which is what reproduces production's
  single origin.
- **Port 8000 must be free.** On this box `milvus-attu` — a container from an unrelated project —
  claims it. `docker stop milvus-attu` (reversible) if the backend cannot bind.

## What it costs

`LLM_BACKEND=openai` with `gpt-4o`, so every run spends a router call, a rerank batch and a
generation. That is deliberate: with a fake model the answer cites *by construction*, so the
citation assertion would be measuring the fixture rather than the product — the argument
§8.65(8) made for §11 row 9.

`EMBEDDING_BACKEND=fake` on this box, so the dense retrieval arm carries no meaning and the
hybrid's **sparse (Postgres FTS) arm** is what finds the passage. Hence the per-run `ANCHOR`: a
lexically distinctive phrase, unique per run so no earlier run's leftovers can satisfy this run's
query. `backend/tests/scenarios/` uses the same device.

## Why Playwright here, when R-66(5) declined it

R-66(5) is **fidelity-scoped**: it ruled computed-style assertions a stronger instrument than
screenshot diffing, because a screenshot "blurs precisely the 264-vs-266px error an assertion
states outright". None of that reasoning touches functional flows. This suite asserts *behaviour
over time* — ingestion completing, an SSE answer arriving, chips landing seconds later — where
the hard part is waiting correctly. `fidelity/`'s zero-dependency CDP driver keeps measuring
pixels; this drives journeys. See **R-78**.

Headless is correct here for the same reason headed is correct there: R-60(4) makes fidelity runs
headed because headless renders no scrollbars and never matches `:focus`, and both are *rendering*
facts this suite never asserts. `E2E_HEADED=1` to watch it.

## The lesson this suite was built on

Six mutations were run against it. **Two survived**, and fixing them changed the test:

- Making `deleteConversation` a no-op — so the `DELETE` never reached the backend — left the
  suite **green**.
- The same was true of the rename's `PATCH`.

Both because the sidebar updates *optimistically*: the row is forgotten the moment the request is
dispatched, so `toHaveCount(0)` was only ever a claim about the client's own state. Every
"did it stick?" assertion is therefore made **after `reloadAndWait()`**, which is the only way to
ask the server what it believes. With that, both mutations fail — and they fail at the reload,
naming the right step.

That is §8.65(5) once more: *it is not enough that an instrument can fail — it must fail for the
reason it claims.* If you add a step here, mutate the thing it asserts and watch it go red before
you trust it.
