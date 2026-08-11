/**
 * FR-HDR-03 — the Dark/Light segmented theme toggle in the chat header.
 *
 * **The one behavioural decision this component makes, which R-58(5) explicitly left to
 * T-504.** FR-HDR-03 specifies *two segments*, while the prototype puts a single `onClick` on
 * the *container* — so in the prototype clicking the already-active segment flips the theme.
 * Shipped here as two independent `<button>`s that each *set* a theme, so clicking the active
 * one is a no-op. Three reasons:
 *
 *  1. It is what FR-HDR-03 says. Clicking the inactive segment still "toggles the theme"
 *     exactly as that requirement's last sentence has it.
 *  2. Under NFR-A11Y-03 the segments must be real buttons, and a button labelled "Dark" that
 *     moves the user *away* from dark is a mislabelled control.
 *  3. NFR-A11Y-06 forbids colour as the sole carrier of information. Which segment is active is
 *     conveyed only by `--panel2`/`--text` against `--muted2` — a pair that requirement itself
 *     enumerates as a failing, accepted contrast exception, naming "the inactive theme-toggle
 *     segment". `aria-pressed` is the non-visual carrier, and it exists only if each segment is
 *     its own control.
 *
 * It is **pixel-identical** to the prototype either way: the two `4px 11px` segments fill the
 * pill exactly, so the hit areas and the rendering are unchanged and T-510 cannot see this.
 *
 * A radio group (`role="radiogroup"` + `role="radio"`) was considered and declined: it announces
 * marginally better but requires a hand-rolled roving-tabindex and arrow-key contract, for a
 * two-option control where toggle-button semantics are a recognised pattern.
 *
 * `useTheme()` is called here **directly**, never threaded through `AppShellProps` — R-58(5)
 * binds this, because the precedent would drag the other nine FR-CST-01 fields through the
 * shell in T-505..T-508.
 */
import styles from './ThemeToggle.module.css';
import { useTheme } from '../theme/useTheme';
import type { Theme } from '../theme/theme';

/** Names the pair as one control; without it a screen reader announces two unrelated toggle
 *  buttons. Not in §9's literal table — invisible to a mouse user, on the R-59 focus-ring and
 *  T-502 landmark-label precedent. */
const GROUP_LABEL = 'Theme';

/** The prototype's segment order and copy (`Dark` then `Light`), which is also the tab order. */
const SEGMENTS: readonly { readonly theme: Theme; readonly label: string }[] = [
  { theme: 'dark', label: 'Dark' },
  { theme: 'light', label: 'Light' },
];

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();

  return (
    <div className={styles.group} role="group" aria-label={GROUP_LABEL}>
      {SEGMENTS.map((segment) => {
        const active = segment.theme === theme;
        return (
          <button
            key={segment.theme}
            type="button"
            className={active ? `${styles.segment} ${styles.segmentActive}` : styles.segment}
            aria-pressed={active}
            onClick={() => setTheme(segment.theme)}
          >
            {segment.label}
          </button>
        );
      })}
    </div>
  );
}
