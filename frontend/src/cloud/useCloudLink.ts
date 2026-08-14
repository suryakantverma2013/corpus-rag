/**
 * FR-AUT-11's linked state — what FR-KBM-06's button branches on.
 *
 * A hook rather than a context, on `useDocuments`' reasoning: the two consumers (the KB modal's
 * footer button and the picker's header) are both one hop from the composition root.
 *
 * It answers one question — *may this user import?* — and the answer decides what the FR-KBM-06
 * button does: open the picker, or start linking. FR-KBM-06 words that as a property of the
 * button rather than of the picker, which is why the status lives out here and not inside the
 * dialog that consumes it.
 */
import { useCallback, useEffect, useState } from 'react';

import { getLinkStatus, startLink, unlinkAccount } from './mutations';

export interface CloudLinkStore {
  /** `false` until the status has been read once — see `loaded`, which is how you tell it apart
   *  from a genuine "not linked". */
  readonly linked: boolean;
  /** The provider-side address, so the picker can say *which* account it imports from. Metadata,
   *  never a credential and never a provider user id. */
  readonly account: string | null;
  /** Whether the status route has answered. Before it has, FR-KBM-06's button must not commit to
   *  either branch — starting a linking redirect for an already-linked user would be a
   *  page-destroying no-op, and it is the one mistake here the user cannot undo with Back. */
  readonly loaded: boolean;
  /** The last failure this surface owns, rendered verbatim (R-57(4)). */
  readonly notice: string | null;
  refresh: () => void;
  /** Leg 1: mint the authorize URL and navigate. Never returns — or returns having failed. */
  beginLink: () => Promise<void>;
  unlink: () => Promise<boolean>;
  dismissNotice: () => void;
}

export interface UseCloudLinkOptions {
  /** Whether there is a session to call the server with (T-509, FR-AUT-07). Without this the
   *  status read fires unauthenticated on mount and its `401` signs out the user whose session
   *  is still resuming — §8.59's defect, in a fourth place. */
  enabled: boolean;
}

export function useCloudLink({ enabled }: UseCloudLinkOptions): CloudLinkStore {
  const [linked, setLinked] = useState(false);
  const [account, setAccount] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  /** Bumped to re-read after linking or unlinking. A counter rather than a boolean because two
   *  consecutive refreshes must both run. */
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!enabled) {
      // A signed-out session knows nothing, and must not keep claiming the previous user's
      // link — `loaded` goes back to false so the button waits rather than guessing.
      setLinked(false);
      setAccount(null);
      setLoaded(false);
      return;
    }
    let cancelled = false;
    void getLinkStatus()
      .then((status) => {
        if (cancelled) return;
        setLinked(status.linked);
        setAccount(status.account ?? null);
        setLoaded(true);
      })
      // An unreadable status leaves `linked` false, so the button offers to link. That is the
      // honest default: linking an already-linked account is idempotent at Keycloak, while
      // opening a picker that cannot list anything is a dead end. `loaded` still flips, or the
      // button would be inert forever after one transient failure.
      .catch(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, generation]);

  const beginLink = useCallback(async () => {
    const result = await startLink();
    if ('url' in result) {
      // A full-page navigation, by necessity: the flow is a redirect chain through Keycloak's
      // login page and Google's consent screen, neither of which can be satisfied by an XHR.
      // The GUI comes back at `?link=…` on a cold load — see `readLinkReturn`.
      window.location.assign(result.url);
      return;
    }
    setNotice(result.detail);
  }, []);

  const unlink = useCallback(async () => {
    const ok = await unlinkAccount();
    if (!ok) {
      setNotice(UNLINK_FAILED);
      return false;
    }
    setLinked(false);
    setAccount(null);
    return true;
  }, []);

  return {
    linked,
    account,
    loaded,
    notice,
    refresh: useCallback(() => setGeneration((n) => n + 1), []),
    beginLink,
    unlink,
    dismissNotice: useCallback(() => setNotice(null), []),
  };
}

/** TBD(§8.4) — the server answers `204` or an `ErrorResponse`, and the `204` path has no body to
 *  render, so this surface owns the failure copy. */
const UNLINK_FAILED = 'Could not disconnect the account. Try again in a moment.';
