/**
 * `GET /api/v1/config` — FR-SYS-03's configured model id, for FR-ANL-02's MODEL card.
 *
 * Here rather than in `src/api/` because the feature folder owns its calls (`auth/session.ts`
 * is the precedent; `src/api/` holds the transport, not the endpoints). Its only consumer today
 * is the stats panel, which is why it sits beside it — **if a second surface needs deployment
 * configuration, lift this out rather than importing `src/stats` from elsewhere.**
 *
 * Read once per session: this is what the operator configured, and it cannot change under a
 * running page without a deploy.
 */
import { useEffect, useState } from 'react';

import { api } from '../api';
import type { Config } from '../api';

export async function fetchConfig(): Promise<Config | null> {
  try {
    const { data, error } = await api.GET('/api/v1/config');
    return error === undefined && data !== undefined ? data : null;
  } catch {
    return null;
  }
}

/**
 * The configured chat model, or `null` until it arrives.
 *
 * `null` is not a failure state the card renders — `StatsPanel` prefers the answering model's
 * own `model_name` anyway (`modelNameOf`), and an em dash for the one frame before this lands
 * is better than a hard-coded id that drifts from `OPENAI_CHAT_MODEL` the day it changes.
 */
export function useConfig({ enabled }: { enabled: boolean }): Config | null {
  const [config, setConfig] = useState<Config | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void fetchConfig().then((loaded) => {
      if (!cancelled && loaded !== null) setConfig(loaded);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return config;
}
