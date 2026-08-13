/**
 * Reads the session context (§4.17). The whole JS-side consumption of FR-AUT-06/07 —
 * `App` branches on `phase`, the sidebar renders `user`, and the two auth surfaces call the
 * three verbs.
 */
import { use } from 'react';
import { AuthContext } from './AuthContext';
import type { AuthContextValue } from './AuthContext';

export function useAuth(): AuthContextValue {
  const value = use(AuthContext);
  if (value === null) {
    throw new Error('useAuth must be used within an <AuthProvider>.');
  }
  return value;
}
