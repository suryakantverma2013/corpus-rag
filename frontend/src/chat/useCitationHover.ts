/**
 * Reads the FR-CIT-03 hover-card context. The `useTheme` shape.
 */
import { use } from 'react';
import { CitationHoverContext } from './CitationHoverContext';
import type { CitationHover } from './CitationHoverContext';

export function useCitationHover(): CitationHover {
  const value = use(CitationHoverContext);
  if (value === null) {
    throw new Error('useCitationHover must be used within a <CitationHoverProvider>.');
  }
  return value;
}
