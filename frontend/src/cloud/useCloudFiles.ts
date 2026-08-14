/**
 * FR-KBM-10's flat list: the search, the pages, and the two `409`s that stop both.
 *
 * The whole surface is one route (`GET /cloud/{provider}/files`) plus the discipline around
 * *when* to call it. That discipline is the substance of this file, and it is not stylistic:
 * `list_cloud_files` and `import_document` share **one `20/minute` per-principal limit**, so a
 * picker that searched on every keystroke would spend the user's import budget on typing and
 * answer `429` before they reached a file. Hence the debounce, and hence pagination is a button
 * rather than a scroll listener.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import type { DriveFile } from '../api';
import { listCloudFiles } from './mutations';

/** FR-AUT-11's refusal, held so the picker can offer Re-link with the server's own words. */
export interface LinkRequired {
  readonly code: string;
  readonly detail: string;
}

export interface CloudFilesStore {
  readonly files: readonly DriveFile[];
  /** True while the *first* page of the current search is loading — the list is replaced, so the
   *  surface shows a loading state rather than stale rows for a query that no longer applies. */
  readonly loading: boolean;
  /** True while a further page is loading. Separate, because the rows already shown stay. */
  readonly loadingMore: boolean;
  /** Whether the search box has settled and a result has arrived at least once. */
  readonly loaded: boolean;
  readonly canLoadMore: boolean;
  /** Non-null when the provider link needs attention. Stops the list entirely. */
  readonly linkRequired: LinkRequired | null;
  /** Everything else the server refused with, rendered verbatim (R-57(4)). */
  readonly notice: string | null;
  readonly search: string;
  setSearch: (value: string) => void;
  loadMore: () => void;
  dismissNotice: () => void;
}

export interface UseCloudFilesOptions {
  /** The picker's open flag. The list is not fetched while it is shut — this route spends a
   *  third party's quota, so it must not run for a surface nobody is looking at. */
  active: boolean;
}

export function useCloudFiles({ active }: UseCloudFilesOptions): CloudFilesStore {
  const [search, setSearch] = useState('');
  const [files, setFiles] = useState<readonly DriveFile[]>([]);
  const [pageToken, setPageToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [linkRequired, setLinkRequired] = useState<LinkRequired | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  /** Bumped by `loadMore`. A counter, so two consecutive requests for the next page both run. */
  const [page, setPage] = useState(0);

  /**
   * The token the *next* page request must carry, read through a ref.
   *
   * Deliberately not an effect dependency: it changes on every response, and depending on it
   * would make the effect re-run after each page and fetch the following one unprompted — an
   * accidental crawl of the user's entire Drive, at 20 requests a minute.
   */
  const nextToken = useRef<string | null>(null);

  // The debounced query. Splitting it from `search` is what makes the input feel immediate while
  // the request rate stays bounded; the effect below depends on this, never on `search`.
  const [query, setQuery] = useState('');
  useEffect(() => {
    if (!active) return;
    const timer = setTimeout(() => setQuery(search), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [active, search]);

  // A new query replaces the list rather than appending to it, so the page cursor resets with
  // it. In its own effect because it must happen *before* the fetch below reads the ref, and
  // React runs effects in declaration order.
  useEffect(() => {
    nextToken.current = null;
    setPage(0);
  }, [query]);

  useEffect(() => {
    if (!active) return;
    const token = page === 0 ? null : nextToken.current;
    // `loadMore` is only offered when a token exists, but a re-render could still land here with
    // none — refusing rather than re-fetching page 1 under the "more" flag keeps the two
    // loading states honest.
    if (page > 0 && token === null) return;

    let cancelled = false;
    if (page === 0) setLoading(true);
    else setLoadingMore(true);

    void listCloudFiles(query, token).then((result) => {
      if (cancelled) return;
      setLoading(false);
      setLoadingMore(false);
      setLoaded(true);

      switch (result.kind) {
        case 'page':
          nextToken.current = result.nextPageToken;
          setPageToken(result.nextPageToken);
          // Replace on the first page, append on the rest — the flat list grows downward and a
          // replace here would make "Load more" look like it had lost everything above it.
          setFiles((current) => (page === 0 ? result.files : [...current, ...result.files]));
          setLinkRequired(null);
          return;
        case 'link-required':
          setLinkRequired({ code: result.code, detail: result.detail });
          setFiles([]);
          setPageToken(null);
          nextToken.current = null;
          return;
        case 'refused':
          setNotice(result.detail);
          return;
        case 'unauthorized':
          // T-509's handler owns this: the session ended, and the whole app is about to change.
          return;
      }
    });

    return () => {
      cancelled = true;
    };
  }, [active, page, query]);

  // Closing the picker forgets the list. It is a third party's data behind a rate limit, and a
  // stale page rendered on reopen would be indistinguishable from a fresh one.
  useEffect(() => {
    if (active) return;
    setFiles([]);
    setSearch('');
    setQuery('');
    setPageToken(null);
    nextToken.current = null;
    setPage(0);
    setLoaded(false);
    setLinkRequired(null);
    setNotice(null);
  }, [active]);

  return {
    files,
    loading,
    loadingMore,
    loaded,
    canLoadMore: pageToken !== null && !loading && !loadingMore,
    linkRequired,
    notice,
    search,
    setSearch,
    loadMore: useCallback(() => setPage((n) => n + 1), []),
    dismissNotice: useCallback(() => setNotice(null), []),
  };
}

/**
 * How long the search box settles before it costs a request. TBD(§8.4).
 *
 * Sized against the rate limit rather than against feel: at `20/minute` shared with import, a
 * 350 ms window turns ordinary typing into one request per phrase and leaves the budget for the
 * imports the search exists to reach.
 */
const SEARCH_DEBOUNCE_MS = 350;
