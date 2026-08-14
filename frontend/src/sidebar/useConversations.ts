/**
 * The FR-SBR-03 list store — `GET /conversations` and the three verbs that change it.
 *
 * Deliberately does **not** own deletion. `DELETE /conversations/{id}` also purges the LangGraph
 * thread and can answer `503` with the chat intact (R-54(5)), and the chat store has to drop
 * that conversation's transcript either way — so the call lives there and this store learns the
 * outcome through `remove`. One verb, one owner.
 *
 * Order is maintained locally by `sortConversations`, which reproduces `list_by_owner`'s
 * `updated_at DESC, created_at DESC`. Re-fetching the list to learn an order we can compute
 * would be a round trip after every rename and every turn.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import type { ConversationDetail } from '../api';
import { createConversation, listConversations, renameConversation } from '../chat/mutations';
import { sortConversations } from './conversations';
import type { SidebarConversation } from './conversations';

export interface ConversationsStore {
  readonly rows: readonly SidebarConversation[];
  readonly loaded: boolean;
  /** FR-SBR-02 — the `201` carries the FR-ANL-03 meter, so the caller can seed it. */
  create: () => Promise<ConversationDetail | null>;
  /** FR-SBR-07 — optimistic, then the `200` body wins and the list re-sorts. */
  rename: (id: string, title: string) => void;
  /** Drop a row the chat store has established is gone. */
  forget: (id: string) => void;
  /** Adopt what a turn changed, without a third GET. */
  patch: (id: string, updated: { updatedAt: string; messageCount: number }) => void;
}

export interface UseConversationsOptions {
  /** T-509/FR-AUT-07 — see `useDocuments`. A hook cannot be called conditionally. */
  enabled: boolean;
}

export function useConversations({ enabled }: UseConversationsOptions): ConversationsStore {
  const [rows, setRows] = useState<readonly SidebarConversation[]>([]);
  const [loaded, setLoaded] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void listConversations()
      .then((outcome) => {
        if (cancelled || outcome.kind !== 'ok') return;
        setRows(sortConversations(outcome.data));
        setLoaded(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const create = useCallback(async (): Promise<ConversationDetail | null> => {
    // No title: FR-SBR-02's "New chat" is `displayTitle`'s fallback for a null one, so sending
    // the label would make an untitled chat indistinguishable from one a user named that.
    const outcome = await createConversation();
    if (outcome.kind !== 'ok' || !mounted.current) return null;
    const created = outcome.data;
    setRows((current) => sortConversations([created, ...current]));
    return created;
  }, []);

  const rename = useCallback((id: string, title: string) => {
    // Optimistic: the row is the user's own text and the round trip is visible in a list they
    // are looking at. `updated_at` is left alone until the server answers, so the row does not
    // jump twice.
    setRows((current) => current.map((row) => (row.id === id ? { ...row, title } : row)));
    void renameConversation(id, title).then((outcome) => {
      if (!mounted.current) return;
      if (outcome.kind === 'ok') {
        const updated = outcome.data;
        setRows((current) =>
          sortConversations(current.map((row) => (row.id === id ? { ...row, ...updated } : row))),
        );
        return;
      }
      // A rename that failed leaves a title the server does not have. `gone` drops the row;
      // anything else re-reads rather than guessing what the old title was.
      if (outcome.kind === 'gone') {
        setRows((current) => current.filter((row) => row.id !== id));
        return;
      }
      void listConversations().then((refreshed) => {
        if (mounted.current && refreshed.kind === 'ok') setRows(sortConversations(refreshed.data));
      });
    });
  }, []);

  const forget = useCallback((id: string) => {
    setRows((current) => current.filter((row) => row.id !== id));
  }, []);

  const patch = useCallback((id: string, updated: { updatedAt: string; messageCount: number }) => {
    setRows((current) => {
      if (!current.some((row) => row.id === id)) return current;
      return sortConversations(
        current.map((row) =>
          row.id === id
            ? { ...row, updated_at: updated.updatedAt, message_count: updated.messageCount }
            : row,
        ),
      );
    });
  }, []);

  return { rows, loaded, create, rename, forget, patch };
}
