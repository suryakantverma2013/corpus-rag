/**
 * Fail fast, and fail *legibly*, when the stack this suite needs is not there (T-603).
 *
 * The failure mode being prevented is specific: without these checks a missing arq worker shows
 * up thirty seconds later as "the document never reached Ready", which reads as an ingestion
 * defect. Every precondition below turns that into one line naming the process to start.
 */

const BASE = process.env.E2E_BASE_URL ?? 'http://localhost:5173';

/** How each dependency is hosted on the dev box, so the message can name the right thing. */
const HOSTING = [
  '  Postgres  — native on :5432',
  '  Keycloak  — native on :8081',
  '  Redis / MinIO / ClamAV — docker containers',
  '  backend, worker and Vite — native processes (below)',
].join('\n');

async function probe(path: string, what: string, hint: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      signal: AbortSignal.timeout(10_000),
    });
  } catch (cause) {
    throw new Error(
      `T-603 cannot run: ${what} is unreachable at ${BASE}${path}.\n\n${hint}\n\nStack layout:\n${HOSTING}`,
      { cause },
    );
  }
  if (!response.ok) {
    // The readiness body names which dependency is down (database / broker / object_storage),
    // and repeating it here saves a round trip through the logs.
    const detail = await response.text().catch(() => '');
    throw new Error(
      `T-603 cannot run: ${what} answered ${response.status}.\n${detail}\n\n${hint}\n\nStack layout:\n${HOSTING}`,
    );
  }
}

export default async function globalSetup(): Promise<void> {
  if (!process.env.CORPUS_PASSWORD) {
    throw new Error(
      'T-603 cannot run: CORPUS_PASSWORD is not set.\n\n' +
        'It is KEYCLOAK_LIVE_ADMIN_PASSWORD in backend/.env — this suite signs in against the ' +
        'real Keycloak as admin@corpus.local, because none of the surfaces it drives exist ' +
        'until it has. Same variable the fidelity harness names.\n\n' +
        "  CORPUS_PASSWORD='…' npm run e2e",
    );
  }

  // The dev server first: everything else is reached through its proxy, so if it is down every
  // other probe would fail for the wrong reason.
  await probe(
    '/',
    'the Vite dev server',
    'Start it with:  cd frontend && npm run dev -- --port 5173 --strictPort',
  );

  await probe(
    '/health/ready',
    'the backend',
    'Start it with:  cd backend && uv run uvicorn app.main:app --loop app.runtime:selector_loop\n' +
      "(the --loop flag is not optional on Windows: psycopg's async driver needs add_reader, " +
      'which the default ProactorEventLoop lacks, and without it the app boots fine and the ' +
      'first chat turn raises CheckpointerConfigError.)',
  );

  // The one a green backend does not imply, and the one whose absence is most misleading: with
  // no worker an upload still answers 202 and simply never leaves `Queued`.
  await probe(
    '/health/ready/worker',
    'the arq worker',
    'Start it with:  cd backend && uv run arq workers.main.WorkerSettings\n' +
      'Without it an upload is accepted and never reaches Ready.',
  );
}
