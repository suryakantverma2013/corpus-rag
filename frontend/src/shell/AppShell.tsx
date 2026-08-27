/**
 * The FR-LAY-01 three-region shell, and nothing else.
 *
 * Slots rather than imports: T-503..T-508 do not exist yet, and a shell that imported them
 * could not be built or tested until they did — it would have to ship six placeholder
 * components that the later tasks then delete, and a half-built `Sidebar.tsx` is
 * indistinguishable from a finished one at review time. Slots also fix the dependency
 * direction (the shell knows no feature; no feature knows the shell) and keep landmark
 * ownership enforceable: a slot cannot accidentally introduce a second <main>.
 *
 * There is no `theme` prop and no theme-context read here. R-58(5) binds this task to render
 * *inside* `ThemeProvider` and to thread nothing through its own props — everything below
 * restyles through the `data-theme` custom properties, which is the whole point of FR-THM-01.
 * Threading it would set the precedent that drags the other nine FR-CST-01 fields through the
 * shell in T-505..T-508.
 */
import type { ReactNode } from 'react';
import styles from './AppShell.module.css';

/**
 * The <main> id, so the NFR-A11Y-04 bypass link has something to target.
 *
 * Exported since T-511: `App` also focuses this element when the login screen is replaced by
 * the shell. Two callers, one id — a second literal is how the skip link and the sign-in
 * hand-off drift onto different elements.
 */
export const MAIN_ID = 'corpus-main';

export interface AppShellProps {
  /**
   * FR-SYS-04. Rendered as the document's only <h1>, visually hidden (NFR-A11Y-03).
   *
   * Required, and deliberately not defaulted here: §9's "Corpus" is resolved once in `App`,
   * because `brandName`'s other consumers — T-503's FR-SBR-01 brand row and T-509's
   * FR-AUT-01 login card — sit on opposite sides of this component, the login screen being
   * outside the shell entirely. One default, one place.
   */
  brandName: string;
  /**
   * FR-LAY-02. `false` **unmounts** the panel (the prototype's `<sc-if>`); it does not hide
   * it. The chat column expands because the 272px flex track ceases to exist. A
   * `display: none` implementation would look identical in jsdom and be wrong in a browser,
   * which is why the test asserts the slot content is gone rather than just the landmark.
   */
  showStats: boolean;
  /** FR-LAY-01 region 1, inside <nav>. T-503. */
  sidebar?: ReactNode;
  /**
   * FR-LAY-01 region 2, inside <main>. The header (T-504), the scrollable message list
   * (T-505) and the composer (T-506) are its three flex children and own their own padding,
   * borders and `flex` — this component supplies only the column direction that FR-LAY-01's
   * table names.
   */
  chat?: ReactNode;
  /**
   * FR-LAY-01 region 3, inside <aside>. T-507 renders the cards only: the prototype puts the
   * panel's padding and gap on the *same element* as its width and left border, so splitting
   * them across two tasks' stylesheets would need an extra wrapper <div> and change the DOM
   * against the pixel authority.
   */
  stats?: ReactNode;
  /**
   * FR-LAY-03. A DOM position, not a portal target — the FR-CIT-03 hover card and the
   * FR-KBM-01 modal are `position: fixed` and place themselves. Rendered last, exactly where
   * the prototype puts them, so an overlay can never land between two landmarks and disturb
   * reading order. (FR-CMP-04's mention menu is *not* here: the prototype makes it
   * `position: absolute` inside T-506's `position: relative` composer.)
   */
  overlays?: ReactNode;
}

export function AppShell({ brandName, showStats, sidebar, chat, stats, overlays }: AppShellProps) {
  return (
    // A plain <div>, deliberately. <section> or <article> here would scope the <aside> below
    // to sectioning content, demoting it from a `complementary` landmark to a generic element
    // — silently, in getByRole and in a real screen reader alike.
    <div className={styles.root}>
      <a className={styles.skipLink} href={`#${MAIN_ID}`}>
        Skip to chat
      </a>

      {/* The document's only <h1> (NFR-A11Y-03). Hidden rather than visible because the
          prototype has no heading anywhere and the brand row it would otherwise occupy
          belongs to T-503 — owning it here is what lets this task assert the invariant. */}
      <h1 className="visually-hidden">{brandName}</h1>

      <nav className={styles.sidebar} aria-label="Conversations">
        {sidebar}
      </nav>

      {/* tabIndex -1 so the skip link's target can actually take focus: <main> is not
          focusable by default and several engines otherwise move focus nowhere at all. It
          adds no tab stop, and the global ring is `:focus-visible` only, so it never paints
          on a click and cannot reach T-510's screenshots. */}
      <main className={styles.chat} id={MAIN_ID} tabIndex={-1}>
        {chat}
      </main>

      {showStats && (
        /* `tabIndex={0}` for the same reason as the FR-ANL-05 sources list inside it (T-720),
           and note this is a *different* value from `<main>`'s `-1` above: that one is a focus
           target that adds no tab stop, this one is a real tab stop.

           **This one axe does not report, and it was fixed on the requirement rather than on a
           red run — measured, not assumed.** The column sets `overflow-y: auto` and StatsPanel
           contains no focusable element at all, so once it overflows there is nothing to give
           it keyboard access. It *does* overflow: measured **686px of content in 640px** on a
           signed-in chat, and — the part worth knowing — that 46px is **constant across
           viewports**, because R-92 clamps the shell at `--app-min-h` and clips the document
           rather than letting the column shrink. axe stays silent throughout, with or without
           this attribute (verified both ways at 1440x900, 1440x420, 1440x260 and at 200%/400%
           zoom): its `scrollable-region-focusable` matcher wants a descendant lying wholly
           outside the region, and a 46px clip never produces one. So the rule's silence here is
           a limit of the heuristic, not evidence of access — without this attribute the bottom
           46px is genuinely unreachable by keyboard, which is WCAG 2.1.1.
           `aria-label` is what keeps it a *named* tab stop. */
        // The two a11y linters disagree here and the WCAG-tagged one wins; see above.
        // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        <aside className={styles.stats} aria-label="Session statistics" tabIndex={0}>
          {stats}
        </aside>
      )}

      {overlays}
    </div>
  );
}
