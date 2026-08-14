/**
 * The `/cloud/*` calls, and the one place their answers are classified.
 *
 * Same two rules as `kb/mutations.ts`, and for the same reasons:
 *
 * - **Branch on `response.status`**, never on `data`.
 * - **Copy is the server's.** Every rejection renders `detail` verbatim; those strings are
 *   `TBD(§8.4)` and R-57(4) says render them, never match on them.
 *
 * The import verb is deliberately **not** here — it is `POST /api/v1/documents/import`, it
 * answers `UploadResponse`, and it is subject to the R-24 lock, so it belongs beside the other
 * four document verbs in `kb/mutations.ts`. What lives here is the link surface and the file
 * list: the routes that are about the *provider* rather than about a document.
 */
import { api } from '../api';
import type { DriveFile, LinkStatus } from '../api';
import { detailOf } from '../api/detail';
import { CLOUD_PROVIDER } from './cloud';

/**
 * The two `409` codes T-214 kept distinct, and §8.53(5) explains why they must stay that way:
 * a link that never existed and a grant the user revoked at Google are different facts, and
 * collapsing them would have this file list contradict the status route it sits beside.
 *
 * Exported because `kb/mutations.ts` branches on the same two codes for the import verb — one
 * definition, so a rename cannot leave one surface recognising them and the other not (the
 * argument `api/detail.ts` makes about itself).
 */
export const ACCOUNT_NOT_LINKED = 'ACCOUNT_NOT_LINKED';
export const CLOUD_ACCESS_REVOKED = 'CLOUD_ACCESS_REVOKED';

/** Whether a `409` code is one this surface can act on. Both lead to the same Re-link button;
 *  only the copy differs, and that copy is the server's. */
export function isLinkRequired(code: string | null): boolean {
  return code === ACCOUNT_NOT_LINKED || code === CLOUD_ACCESS_REVOKED;
}

/**
 * A page of the flat list, or the reason there is none.
 *
 * `link-required` is not an error: for a user who has never linked, it is the *ordinary* state
 * and the surface's whole job at that moment is to offer linking. `refused` carries everything
 * else, including `429` — which matters here because this read shares the upload rate limit.
 */
export type ListResult =
  | { kind: 'page'; files: readonly DriveFile[]; nextPageToken: string | null }
  | { kind: 'link-required'; code: string; detail: string }
  | { kind: 'refused'; detail: string; status: number }
  | { kind: 'unauthorized' };

const NETWORK = 'Could not reach the server. Check your connection and try again.'; // TBD(§8.4)

interface RawResult {
  status: number;
  error?: unknown;
  data?: { files?: readonly DriveFile[]; next_page_token?: string | null } | undefined;
}

/** Pure, so every documented status is table-testable with no transport. */
export function classifyList(result: RawResult, fallbackDetail: string): ListResult {
  const { status } = result;
  const detail = detailOf(result.error);

  if (status === 200) {
    const files = result.data?.files;
    // A `200` with no readable body cannot happen against a current server. Treating it as an
    // empty page rather than a refusal keeps the surface usable if it ever did.
    return {
      kind: 'page',
      files: files ?? [],
      nextPageToken: result.data?.next_page_token ?? null,
    };
  }
  if (status === 401) return { kind: 'unauthorized' };
  if (status === 409 && isLinkRequired(detail.errorCode)) {
    return {
      kind: 'link-required',
      code: detail.errorCode as string,
      detail: detail.message ?? fallbackDetail,
    };
  }
  return { kind: 'refused', detail: detail.message ?? fallbackDetail, status };
}

/** Run a call and normalise a thrown transport failure into the same shape. */
async function attempt(run: () => Promise<RawResult>): Promise<ListResult> {
  try {
    return classifyList(await run(), NETWORK);
  } catch {
    return { kind: 'refused', detail: NETWORK, status: 0 };
  }
}

/**
 * `GET /cloud/{provider}/files` — FR-KBM-10's flat list.
 *
 * `page_size` is left to the server (50, `CLOUD_LIST_PAGE_SIZE`): it is a product bound the
 * backend already owns, and a client that names its own would silently stop tracking it.
 */
export function listCloudFiles(search: string, pageToken: string | null): Promise<ListResult> {
  return attempt(async () => {
    const { data, error, response } = await api.GET('/api/v1/cloud/{provider}/files', {
      params: {
        path: { provider: CLOUD_PROVIDER },
        // Empty string is sent as absent: the backend builds a `name contains ''` clause from
        // it otherwise, which is a filter that matches everything by accident rather than by
        // intent, and it makes the "no search" request differ from itself between renders.
        query: {
          search: search.length === 0 ? null : search,
          page_token: pageToken,
        },
      },
    });
    return { status: response.status, error, data };
  });
}

/**
 * `GET /cloud/links/{provider}` — FR-AUT-11's linked state.
 *
 * Throws rather than returning a result union, on `listDocuments`' precedent: an unreadable
 * status is not a user-facing outcome, it just means the button keeps offering to link, which
 * is the honest default for a state we could not read.
 */
export async function getLinkStatus(): Promise<LinkStatus> {
  const { data, error, response } = await api.GET('/api/v1/cloud/links/{provider}', {
    params: { path: { provider: CLOUD_PROVIDER } },
  });
  if (error !== undefined || data === undefined) {
    throw new Error(`cloud link status failed: ${response.status}`);
  }
  return data;
}

/**
 * `POST /cloud/links/{provider}/start` — mint leg 1's authorize URL.
 *
 * Returns the URL rather than navigating: the caller decides, and a function with a side effect
 * on `window.location` cannot be tested. `null` when the server refused, which the caller
 * renders as a notice rather than navigating to nowhere.
 *
 * No provider call happens behind this route, so it cannot fail on Keycloak or Google being
 * down — but it *is* rate limited, and a `429` here has to be visible rather than silent.
 */
export async function startLink(): Promise<{ url: string } | { detail: string }> {
  try {
    const { data, error, response } = await api.POST('/api/v1/cloud/links/{provider}/start', {
      params: { path: { provider: CLOUD_PROVIDER } },
    });
    if (response.status === 200 && data !== undefined) return { url: data.authorize_url };
    return { detail: detailOf(error).message ?? NETWORK };
  } catch {
    return { detail: NETWORK };
  }
}

/**
 * `DELETE /cloud/links/{provider}` — FR-AUT-11's unlink. Idempotent (`204` twice).
 *
 * `true` on success. Documents already imported are untouched by design — they are copies
 * (FR-KBM-10), so this revokes future import and nothing else, and the confirmation copy says
 * exactly that.
 */
export async function unlinkAccount(): Promise<boolean> {
  try {
    const { response } = await api.DELETE('/api/v1/cloud/links/{provider}', {
      params: { path: { provider: CLOUD_PROVIDER } },
    });
    return response.status === 204;
  } catch {
    return false;
  }
}
