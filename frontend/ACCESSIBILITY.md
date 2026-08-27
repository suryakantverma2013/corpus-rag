# Corpus — Accessibility conformance statement

**Target:** WCAG 2.2 Level AA.
**Status:** met for motion, focus, semantics, keyboard operability and programmatic announcement. **Not met for colour contrast**, which is an enumerated, accepted exception — see [Contrast](#contrast-the-accepted-exceptions) below. Any procurement questionnaire or VPAT must cite that section.

Requirements: `NFR-A11Y-01..06` (spec §5.8), ruled in §8.42 (R-59), §8.43 (R-60) and §8.62 (R-75).
Audited: **2026-08-14**, tasks T-511 and T-514 (§8.63), against the full live stack (Keycloak, API, arq worker, PostgreSQL, MinIO).

---

## How this was audited

Every check below ran in a **headed** browser. Headless is inadmissible here and that is a measured constraint, not a preference: headless Chromium and Firefox render **no scrollbars at all** (every width 0px, including an element forced to `scrollbar-width: auto`), and headless Chromium never matches `:focus` because the page has no window focus. A 0px scrollbar reading is inconclusive, never a pass (R-60(4)).

| Instrument                                                                         | What it covered                                                                      |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `axe-core` 4.13, injected over CDP by **`npm run a11y`** (`frontend/a11y/`, T-614) | 10 surfaces × 2 themes (see coverage below) — **59 checks, re-runnable**             |
| Raw CDP keyboard driving, no pointer input                                         | the full flow: login → upload → ask → cite → feedback → regenerate → rename → delete |
| OS-level pointer input (DPI-aware, two-point calibrated)                           | `:focus-visible` must **not** paint on a mouse click                                 |
| `Emulation.setEmulatedMedia`                                                       | `prefers-reduced-motion`                                                             |
| Firefox 153 + `ui.prefersReducedMotion`                                            | cross-engine motion and scrollbars                                                   |
| CDP Accessibility domain                                                           | live-region roles and politeness                                                     |
| `oxlint --jsx-a11y-plugin`, `vitest`                                               | static and regression coverage                                                       |

---

## Automated results (axe-core)

**Ten surfaces, each in dark and light:** login, shell, conversation `⋯` menu, user menu, change-password modal, knowledge-base modal, citation hover card, mention menu (T-511), plus the **FR-KBM-10 cloud picker** and its **unlink `ConfirmDialog`** (T-514).

**Coverage is complete: every §4 surface in the product appears above.** The two T-511 left uncovered were both swept by T-514, against a real linked Google Drive account:

- **The FR-KBM-10 cloud picker** — T-511 recorded it as unopenable because the audit account was unlinked. That was already untrue: the account has been federated since T-214, so the picker opens against the live provider. Scoped to its own panel it reports **`color-contrast` and nothing else**, 20 nodes per theme.
- **The `ConfirmDialog`** — and this is where the one wrong claim in this document came from. See [Contrast](#contrast-the-accepted-exceptions).

_The lesson is about the two gaps, not the surfaces: T-511 gave one of them a board task and left the other as prose, and the prose one is the one that produced a false statement below. A caveat with no owner is not a caveat — it is a gap that gets quoted as a fact._

**Exactly three rules fire, and only one is WCAG-tagged.**

| Rule                | axe tags             | Nodes | Disposition                                                                        |
| ------------------- | -------------------- | ----- | ---------------------------------------------------------------------------------- |
| `color-contrast`    | `wcag2aa`, `wcag143` | 559   | **Accepted** — every node maps to an NFR-A11Y-06 enumerated group (below)          |
| `region`            | `best-practice`      | 22    | **Accepted** — the shell's visually-hidden `<h1>` and the citation tooltip (below) |
| `landmark-one-main` | `best-practice`      | 0     | **Fixed** during this audit                                                        |

No other rule reports a violation on any surface in either theme. There is **no WCAG 2.2 AA violation outside the accepted palette exceptions**.

> **This claim was false when written, and T-614 found it by making the pass runnable.** The
> statement above covers WCAG 2.2, but the T-511/T-514 runs did not include the `wcag22aa` tag, so
> **`target-size` (2.5.8) was never evaluated**. `npm run a11y` includes it, and its first run
> reported the login screen's password **Show/Hide** button at **39.6 x 19 px** — under the 24 x 24
> minimum, and failing the spacing exception too (19 px safe clickable diameter against 24 px).
> Fixed in `LoginScreen.module.css` by giving it `min-height: 24px` and centring the label: the
> text does not move, because the button is absolutely positioned and re-centred by
> `translateY(-50%)`. The §4.17 auth UI is NFR-VIS-01's Rev 0.6 carve-out, where the spec is the
> baseline rather than the prototype's pixels, so enlarging a hit area here is not a fidelity
> change. **The claim now holds because it is checked, rather than because it was written down.**

### What axe cannot decide here

- **`--line` borders (NFR-A11Y-06's fifth group, 1.18–1.23:1) are not covered.** WCAG 1.4.11 non-text contrast is not automatable, and axe does not attempt it. That group remains verified by the original R-59 measurement, not by this pass.
- **`color-contrast` is evaluated per element, not per token pair**, so 559 nodes collapse to 29 distinct foreground/background/size combinations, all of which are the same handful of tokens on different surfaces.
- **Run over the whole document with a modal open, axe measures the content behind the scrim.** With the unlink confirmation up, the picker's rows and the sidebar report **1.01:1** — light `--text` against the overlay's dark wash. It reads as a catastrophic defect and means nothing: that text is obscured by design. **Scope the probe to the panel under test** (`axe.run(panel)`), which is how T-514's picker figures were taken. **Re-measured at axe-core 4.13 by T-614 and the symptom no longer reproduces**: obscured nodes now land in `incomplete` (_"background color could not be determined because it partially overlaps"_) rather than in violations — with the unlink confirmation open, unscoping the probe changed no result and produced no sub-1.5:1 violation. The scoping rule stands on what a surface under test _means_, and because `incomplete` handling is a library detail that can move again; it no longer stands on a symptom you can still see.

---

## Contrast: the accepted exceptions

The §5.1 palette is fixed by NFR-VIS-01/NFR-VIS-02. Measured against WCAG 1.4.3 (4.5:1 — all UI text here is below 18.66px) and 1.4.11 (3:1), **12 of 28 token pairs fail in dark and 18 of 28 in light**. Closing them means changing token values, which is a visual redesign the design handoff has not authorised, so they are **accepted, recorded exceptions rather than defects** (NFR-A11Y-06).

Two mitigations are required and are in force:

1. **Colour is never the sole carrier of information.** FR-EVL-02 renders the numeral beside the hue; FR-KBM-04 renders a text label beside the status colour; FR-HDR-03 and FR-KBM-05 carry `aria-pressed` rather than relying on the active segment's colour.
2. **No new colour pair may be introduced below threshold.** The exception grandfathers the existing palette only.

### Measured, this pass

| Group (NFR-A11Y-06)           | Measured                             | Notes                                                                                     |
| ----------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------- |
| `--muted2` as text            | dark 2.80–3.17:1 · light 2.24–2.60:1 | Matches the enumerated 2.19–3.18:1. Carries section labels, mono badges, the version tag. |
| `#fff` on `--accent`          | dark 3.15–3.17:1                     | Matches. Send glyph, brand mark, AI avatar.                                               |
| `--accent` as text            | light 3.56–4.11:1 · **dark 4.31:1**  | **Two refinements** — see below.                                                          |
| FR-EVL-03 status hues as text | **light 3.06:1** (`--eval-bad`)      | **Observed after all** — see below. T-511 reported this group as not occurring.           |
| `--line` borders              | not automatable                      | Unchanged from R-59's measurement.                                                        |

**Two refinements to the enumeration, recorded rather than silently absorbed.** Neither is a new pair — both are the existing palette on a surface the enumeration did not name — so both remain accepted:

- **`--accent` on `--accent-soft` fails in the dark theme too**, at 4.31:1 (9.5px, the citation chip). NFR-A11Y-06 enumerates `--accent`-as-text for the light theme only.
- **The light-theme `--accent` failures reach 3.56:1**, below the enumerated 3.92:1 floor, on composited surfaces (`--accent-soft` over `--bubble`).

**The FR-EVL-03 hues DO occur as text — this corrects T-511 (spec §8.63(5)).** T-511 reported the group as not occurring at all, on the strength of the eval chip, which does render its numeral in `--muted` (≈5.9:1, passing) with the hue confined to an `aria-hidden` dot. But it had not scanned `ConfirmDialog`, and **`ui/ConfirmDialog` colours its destructive confirm button with `color: var(--eval-bad)`** — measured **3.06:1** in the light theme, at the top of the enumerated 2.15–3.06:1 range. It is the product's only confirmation primitive, so the pair ships on **every destructive confirmation**: delete conversation (FR-SBR-07), delete document (FR-KBM-07) and unlink (FR-AUT-11).

**It remains an accepted exception, and the mitigation is doing its job:** the button's own label states the action (`Disconnect`, `Delete`) and the dialog states the consequence above it, so colour carries nothing on its own. Nothing needs to change — but a conformance statement must cite this group as **live**, not latent. _T-511's own caveat predicted the mechanism and guessed the wrong instance: it expected FR-KBM-04's `Failed` label. That label is still unobserved; the destructive button is what makes the group live._

### Accepted `region` findings

- **The shell's visually-hidden `<h1>`** sits outside every landmark. Deliberate (T-502 owns the document's only `<h1>` at the shell root, `position: absolute` so it is not a fourth flex item). `region` is an axe best-practice rule, not a WCAG criterion.
- **The FR-CIT-03 citation card** renders into the `overlays` slot, outside the landmarks, for reading-order reasons. It is a `role="tooltip"` reached through `aria-describedby` from its chip — landmark navigation is not its access path.

---

## Defects found and fixed

| #   | Finding                                                                                                       | Requirement | Fix                                                                                                  |
| --- | ------------------------------------------------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------- |
| 1   | Tab walked out of both open `role="menu"` popovers; the sidebar's then could not be closed by keyboard at all | NFR-A11Y-04 | Both dismiss on Tab and restore focus to the trigger                                                 |
| 2   | Signing in left `document.activeElement` on `<body>`                                                          | NFR-A11Y-04 | `App` focuses `<main>` on the transition, reusing the skip link's target                             |
| 3   | The login screen had no landmark; all 8 of its elements sat outside one                                       | NFR-A11Y-03 | Its centring container is a `<main>` — zero pixels                                                   |
| 4   | The repo-wide `outline: none` prohibition was enforced over one stylesheet                                    | NFR-A11Y-02 | Repo-wide guard, three sanctioned sites enumerated; exception written into the requirement (R-75(1)) |
| 5   | The FR-KBM-10 picker opened with focus on the header ✕, so its arrow keys were bound to nothing (T-514)       | NFR-A11Y-04 | The search input takes initial focus; `searchRef` had been wired since T-512 and never read          |

**On (1), the interesting part:** the two menus were built by different tasks and handle keys differently — the sidebar's on the menu container (bubbling, so it only fires while focus is inside it), the user menu's on `document` (capture). Neither treated Tab, and its items are ordinary tab stops. The user menu survived the escape because its listener still ran; the sidebar's did not, so Escape went to whatever the browser had focused instead and the popover became unclosable. Two implementations of one pattern diverged exactly where neither task's own tests looked.

---

## Verified behaviour

### Keyboard (NFR-A11Y-04)

Driven with key events only — no pointer input anywhere in the traversal.

- FR-AUT-02 tab order is **email → password → Show/Hide → Sign in**, with email autofocused.
- 26 tab stops across the shell, no trap, wrapping cleanly — as measured on T-511's fixture. **The total is content-dependent** (six conversations with their per-row FR-SBR-07 affordances, one FR-MSG-08 action bar per answer, one chip per citation), so treat it as that run's figure rather than an invariant; a populated chat walks well past it.
- **T-720 adds two stops, and both are scroll containers rather than controls**: the `.stats` column (`aside[aria-label="Session statistics"]`) and the FR-ANL-05 sources list inside it. Each sets `overflow-y: auto` and contains **nothing focusable**, so without `tabIndex={0}` neither can be scrolled by keyboard. The sources list was reported by `npm run a11y` as `scrollable-region-focusable` (WCAG 2.1.1/2.1.3, Level A) once R-92(3) capped it at three rows; **the column was not**, and that is worth knowing — it overflows by a measured **46px (686 in 640)**, constant across viewports because R-92 clamps the shell at `--app-min-h`, and axe's matcher wants a descendant lying wholly outside the region, which a 46px clip never produces. Verified both ways at 1440×900, 1440×420, 1440×260 and at 200%/400% zoom: axe is silent with *and* without the attribute there. **The rule's silence is a limit of the heuristic, not evidence of access** — the fix stands on WCAG 2.1.1 itself, and `AppShell.test.tsx` is the only thing guarding it. `oxlint`'s `jsx-a11y/no-noninteractive-tabindex` disagrees at both sites and is suppressed inline with the reason: the two linters conflict here and the WCAG-tagged one wins.
- Hover-revealed affordances are reachable: the FR-SBR-07 `⋯` control (6 instances) and the FR-MSG-08 action bar (👍 / 👎 / Regenerate).
- The FR-CIT-01 citation chip is a real tab stop; focusing it opens the FR-CIT-03 card and wires `aria-describedby`; Escape closes it.
- The KB modal traps focus, wraps in both directions, closes on Escape and restores focus to its opener.
- **Nested dialogs behave correctly, verified live rather than left to the suite.** With the delete confirmation open over the KB modal, one Escape closes **only the topmost** dialog, the modal survives, and focus returns _into it_ (onto the Delete button) rather than to the document; a second Escape closes the modal and restores focus to the control that opened it. Cancel takes initial focus, so a stray Enter dismisses rather than deletes. This is checked live deliberately: the T-508 defect here was two capture-phase `document` listeners firing in registration order, and §8.58(5)(a) records that **a green end state is not evidence when two handlers race** — so the assertion is on the intermediate state, not just the final one.
- Both menus focus their first item, cycle and wrap with the arrow keys, and restore focus to the trigger on close.
- FR-CMP-05: the mention menu opens with **no active option**, so Enter still sends (FR-CMP-03); ArrowDown moves virtual focus via `aria-activedescendant` while DOM focus stays in the input; Escape closes it.
- **The composer field is a plain `textbox`, not a combobox (R-96, Rev 0.63)** — and unlike the cloud picker below, which is an `<input>` and keeps the role legitimately. R-91 made this control a `<textarea>`, where ARIA 1.2 permits neither `role="combobox"` nor `aria-expanded`; **removing only the role exchanges one axe violation for another**, which is why both are absent and must stay absent together. `aria-controls`, `aria-autocomplete` and `aria-activedescendant` are all still permitted, so the keyboard contract above is unchanged. The expanded state moves to a polite live region — `{N} document(s) available. Use the arrow keys to reference one.` — rendered always and emptied on close, because a region mounted with text already in it is frequently never announced. The `@` button keeps its own `aria-expanded` as a second carrier that speaks when focus is on it.
- Send is absent from the tab order **only while the composer is empty**, because FR-CMP-03 disables it; it becomes a tab stop as soon as there is text.
- The skip link is the first tab stop, reveals itself on focus, and moves focus to `<main>`.

### The FR-KBM-10 cloud picker (T-514)

Swept against a real linked Google Drive account, 50 real files listed. 27 keyboard/ARIA checks, then 13 more across a real import.

- The search input takes initial focus, so the arrow keys work on open (fixed here — see defect 5).
- It is a `role="combobox"` with `aria-autocomplete="list"`, `aria-controls` naming the listbox, and a visually-hidden `<label>`; the listbox is `aria-labelledby` its section heading.
- **No option is active on open**, so Enter still does nothing until the user deliberately arrows — the FR-CMP-03 conflict resolved the same way as the mention menu.
- ArrowDown/ArrowUp move `aria-activedescendant` and wrap, while **DOM focus stays in the input**; the active row carries `aria-selected="true"`.
- Rows are `<button role="option" tabIndex={-1}>`, so **tabbing past fifty files is not required**; Tab never leaves the panel and wraps.
- **An imported row keeps `aria-disabled` and never gains `disabled`** (R-74(8)), verified with a real import driven entirely from the keyboard: it still takes virtual focus, still answers `aria-selected`, and CDP still reports it as `option` with `disabled: true`. Re-activating it is a no-op. _This is the whole reason for the rule — a `disabled` button would have dropped out of the arrow-key traversal it stays in here._
- **The nested Escape stack holds three dialogs deep** (knowledge-base modal › picker › unlink confirmation): one Escape closes only the confirmation and returns focus _into_ the picker; the next closes the picker and restores focus to the button that opened it. Cancel takes initial focus. Asserted on the intermediate states, not just the end state.

### Motion (NFR-A11Y-01)

Under `prefers-reduced-motion: reduce`, in both themes and **both engines**:

- `--motion-fast`, `--motion`, `--motion-slow`, `--motion-bar` all compute to `0s`.
- `--motion-dot` and `--motion-dot-delay` are deliberately **not** zeroed (R-59(2)).
- `dotPulse` holds **opacity 1** even against a `.25` base style — the exact failure mode R-59(2) measured, and the reason the keyframe is redefined inside the media query rather than switched off.
- `fadeUp` arrives in place: opacity 1, no translation.
- Both keyframe names resolve to the real global keyframes, not CSS-Modules hashes (the T-505 defect class).

### Focus indicator (NFR-A11Y-02)

Same control, both themes, real OS pointer input vs keyboard:

| Input            | `:focus` | `:focus-visible` | Outline                                                                    |
| ---------------- | -------- | ---------------- | -------------------------------------------------------------------------- |
| Real mouse click | true     | **false**        | `none`                                                                     |
| Tab              | true     | **true**         | `2px solid` — `rgb(124,134,248)` dark, `rgb(91,102,232)` light, 2px offset |

This is T-510's precondition: the ring cannot appear in a screenshot taken the way that task takes them.

### Scrollbars (NFR-USE-05)

Measured headed under forced overflow, borders subtracted:

- **Chromium**: all three real scroll containers (sidebar list, message list, stats panel) = **8px**, both themes. The stats panel reads 9 until its 1px left border is subtracted.
- **Firefox 153**: `scrollbar-width` computes `thin` = **8px**. R-60's two corrections re-confirmed — Gecko still reports the bare `::-webkit-scrollbar` as _supported_ (so the gate must key on `-thumb`, which it does), and the gate reaches real scroll containers.

### Live regions (NFR-A11Y-05)

Across a real turn on the live stack:

- Every live region is `role="status"`, `aria-live="polite"`, visually hidden, and present in the accessibility tree as a polite live region (confirmed via CDP, not inferred from attributes).
- An arriving answer announces **`Answer received.`** — the fixed phrase, not the answer text (R-69(1)).
- It announces **once**, from the message list's region, not once per mounted region — three can be mounted simultaneously.
- The FR-CMP-06 attachment count deliberately carries no `aria-live`: it changes only by direct user action.

---

## Standing rules for future work

1. **Never write a component-level `prefers-reduced-motion` block, and never hard-code a duration.** `tokens.css` owns reduced motion globally; a piecemeal block is how `dotPulse` gets broken in one file and not another. Enforced by a guard in every `*.css.test.ts`.
2. **Never write `outline: none`** unless the indicator is relocated _and_ the site is added to the enumerated set in `src/styles/tokens.test.ts`.
3. **Never reference a keyframe from a `*.module.css`** — CSS Modules rewrites `animation-name` even for keyframes it does not declare, so the animation silently never runs. Use `.animate-fade-up` / `.animate-dot-pulse`. Enforced repo-wide.
4. **`aria-disabled`, never `disabled`, on a listbox option** — a disabled button leaves the accessibility interaction model, so the row stops being reachable by the arrow keys that move virtual focus.
5. **No new colour pair below 4.5:1 text / 3:1 non-text.** The recorded exceptions grandfather the existing tokens only.
6. **Scrollbar and focus checks run headed.** 0px is inconclusive; `:focus` never matches without window focus.
7. **A dialog that owns a keyboard contract must take focus where that contract is bound.** `ui/Dialog` focuses the first focusable control, which is right for a form and wrong for a combobox whose arrow keys live on an input further down. If a surface moves focus itself, say why at the call site — and remember that a test which focuses the control before pressing a key cannot tell you whether the product ever focused it.
