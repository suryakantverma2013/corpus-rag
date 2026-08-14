/**
 * FR-KBM-10 / FR-AUT-11's decidable half — the picker's derivations and the link return.
 *
 * Pure: no React, no `fetch`, no DOM. Everything the requirement *enumerates* lives here so it
 * is testable without rendering anything, on the `mentions.ts` / `documents.ts` precedent; the
 * component keeps focus, ARIA and paint, and `mutations.ts` keeps the transport.
 */
import { nextActiveIndex } from '../composer/mentions';
import type { DriveFile } from '../api';

/**
 * The one provider FR-KBM-10 commits to in v1.
 *
 * A named constant rather than a bare `'google'` at six call sites because it is a *path
 * segment* the backend validates as an enum (R-63(6)(1)'s discipline one level up from the file
 * id) — so when NFR-CMP-02's second provider arrives, this is the binding that has to widen and
 * TypeScript will name every site.
 */
export const CLOUD_PROVIDER = 'google';

/** What the provider is called in copy. Separate from the id above: one is a wire value. */
export const CLOUD_PROVIDER_NAME = 'Google Drive';

/**
 * Ids `aria-controls` / `aria-activedescendant` and the listbox's own elements must agree on.
 *
 * Here rather than in the component for the reason `mentions.ts` records: a module that exports
 * both a component and a value trips `react/only-export-components`.
 */
export const CLOUD_LIST_ID = 'cloud-file-listbox';
export const CLOUD_HEADING_ID = 'cloud-file-heading';

export function cloudOptionId(index: number): string {
  return `cloud-option-${index}`;
}

/**
 * The active-row index after an arrow key.
 *
 * Imported from the composer rather than restated: the wrap rule and the "-1 means nothing is
 * active" convention are NFR-A11Y-03's listbox contract, not the mention menu's private
 * behaviour, and two copies is how one surface starts wrapping and the other stops. The
 * cross-folder import matches `documents.ts` reaching into `stats.ts` for `documentTypeBadge`.
 */
export { nextActiveIndex as nextCloudIndex };

/**
 * The closed vocabulary Keycloak returns the browser with (§9, T-214).
 *
 * `denied` is only ever produced by leg 1 — the user cancelled at the consent screen — while
 * leg 2 answers only `linked` or `failed`. The GUI branches on these three and never on copy.
 */
export type LinkOutcome = 'linked' | 'failed' | 'denied';

const LINK_OUTCOMES: readonly string[] = ['linked', 'failed', 'denied'];

/**
 * Read the linking outcome off a location search string, or `null` if this is an ordinary load.
 *
 * Takes the string rather than reading `window.location` so it is testable and so the caller
 * decides *when* it is read — which matters, because the caller reads it once at mount and then
 * strips it (§8.59: StrictMode runs effects twice, and a second read after the strip must be a
 * clean `null` rather than a second notice).
 *
 * An unrecognised `link=` value yields `null`: the vocabulary is closed, and inventing a fourth
 * state from whatever arrived in the query string is how a stray URL opens a surface.
 */
export function readLinkReturn(search: string): LinkOutcome | null {
  const value = new URLSearchParams(search).get('link');
  return value !== null && LINK_OUTCOMES.includes(value) ? (value as LinkOutcome) : null;
}

/**
 * FR-KBM-04's meta line, for a file that is not a document yet.
 *
 * Deliberately the *same shape* as `documents.ts::metaLine` — ` · `-joined clauses, each dropped
 * when its field is absent — because these two lists sit one above the other in the same modal
 * and a second layout convention would read as two different kinds of thing. What differs is the
 * content, and it has to: a Drive file has no chunk count and no indexed date, so this is size
 * and last-modified, both of which the provider supplies and either of which may be null.
 *
 * Returns `''` when the provider gave neither, which the component renders as no line at all.
 */
export function driveMeta(file: DriveFile): string {
  const parts: string[] = [];
  const size = fileSize(file.size_bytes);
  if (size !== null) parts.push(size);
  const modified = modifiedOn(file.modified_time);
  if (modified !== null) parts.push(`modified ${modified}`);
  return parts.join(' · ');
}

/** Binary units, to agree with FR-ERR-01's ceiling — `UPLOAD_MAX_FILE_BYTES` is 50 × 1024². A
 *  decimal formatter would call a file the server refuses at 50 MB "49.6 MB". */
const UNITS = ['bytes', 'KB', 'MB', 'GB'] as const;

/**
 * `null` for an absent size — Drive omits it for some items, and "0 bytes" would be a claim.
 *
 * Bytes render whole (a "0.4 KB" file is noise); everything above renders to one decimal, which
 * is what makes the 50 MB ceiling legible at the point the user is choosing.
 */
export function fileSize(bytes: number | null): string | null {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return null;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rendered = unit === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${rendered} ${UNITS[unit]}`;
}

/**
 * The provider's RFC-3339 `modifiedTime`, rendered in `documents.ts::indexedOn`'s format.
 *
 * **Locale pinned**, the same reason that function pins it: an unpinned `toLocaleDateString`
 * renders `08 Jul` in half the world and `Jul 08` in the other, so the two lists in this modal
 * would disagree with each other on the same machine.
 *
 * `null` for an absent or unparseable value — `Invalid Date` must never reach a row.
 */
export function modifiedOn(iso: string | null): string | null {
  if (iso === null) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleDateString('en-US', { month: 'short', day: '2-digit' });
}
