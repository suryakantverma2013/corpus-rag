# Corpus GUI (frontend)

The "Corpus" web UI for the Nexus AI RAG platform. **React + Vite + TypeScript**
(stack locked by spec R-16 / R-26 — Next.js declined). Currently: the T-004 token
scaffold, the T-405 generated API client, T-501's theme runtime and T-502's three-column
shell. The components that fill it follow in T-503..T-509.

## Design authority

The UI is **pixel-perfect** (NFR-VIS-01) to the prototype:

> `../RAG Chatbot GUI Design/design_handoff_rag_chatbot/RAG Chatbot.dc.html`

When instinct and the prototype disagree, the prototype wins. The only exceptions
are the Rev 0.5/0.6 additions where the spec (`../Nexus_AI_Detailed_Specification.md`)
is the baseline. See the `frontend-dev` skill for the full convention list.

> **⚠ The prototype cannot render its own data** (found in T-503). It loads
> `<script src="./support.js">` and **that file is not in the handoff** — so opened in a
> browser, every binding shows its literal source (`{{ brandName }}`, `{{ c.title }}`),
> `<sc-for>` never expands, and `<sc-if>` regions — the KB modal, the citation hover card —
> never appear. **Compare against its inline style declarations, which are readable from
> source and are what NFR-VIS-02/04 enumerate; do not trust a screenshot of anything whose
> size depends on content.** T-503 was misled three times before checking: the 14-character
> `{{ docCount }}` wrapped and made the KB button 52px instead of 36px, `{{ brandName }}`
> made the brand span 122px instead of 55px, and an unexpanded `<sc-for>` swallowed the
> conversation list's `gap: 2px`. Static chrome still measures true — T-502's 265/844/309
> column check matched exactly. See the T-510 note on the board.

## Design tokens

All colors/motion/radii live in **`src/styles/tokens.css`** — the machine copy of
spec §5.1. Both themes are defined on the root element and selected by a
`data-theme="dark|light"` attribute (dark is the default). Never hard-code a color;
use a token. Fonts (Instrument Sans + JetBrains Mono) load from Google Fonts in
`index.html`.

## Theming at runtime (T-501, ruling R-58)

`src/theme/` owns the runtime half. Four single-responsibility modules and **no barrel** —
`react/only-export-components` fires on any module exporting both a component and something
else, which is exactly what a barrel re-exporting `ThemeProvider` _and_ `useTheme` would be.

```tsx
import { ThemeProvider } from './theme/ThemeProvider'; // { accent?: string; children? }
import { useTheme } from './theme/useTheme';
const { theme, isDark, isLight, setTheme, toggleTheme } = useTheme();
```

Four rules, each of which has already caught something:

- **Persistence** is `localStorage['corpus.theme']` (`dark` | `light`); anything absent,
  unrecognised or unreadable falls back to `dark` (NFR-USE-01). `index.html` carries a
  parser-blocking inline script that applies it **before first paint** — it duplicates
  `readStoredTheme()` because it cannot import it, and `theme.test.ts` pins the key so the two
  cannot drift. Change the key in one place and the test names the other.
- **`accent` has no default here, on purpose.** FR-SYS-04's `#7C86F8` is the `--accent` token,
  per theme. Writing a JS default would put the dark accent on `:root`, where it outranks
  `:root[data-theme='light']`, and destroy the `#5B66E8` NFR-VIS-02 specifies for light — a bug
  no type-check, lint or dark-theme screenshot can see. `main.tsx` renders `<App />` with no
  props; keep it that way.
- **`--accent-soft` is a literal by default and derived only under an override.** It is the only
  token reading `--accent`, so an override that skipped it would leave the old theme's tint
  behind every accent element. It is not derived _permanently_ because a computed colour renders
  1/255 off a legacy `rgba()` in Chromium, and NFR-VIS-01 is a pixel requirement. The per-theme
  alpha lives in `--accent-soft-alpha`.
- **The scrollbar `@supports not selector(::-webkit-scrollbar-thumb)` gate is load-bearing, and
  the `-thumb` matters.** Chrome 121+ ignores `::-webkit-scrollbar` on any element that also
  sets `scrollbar-width`/`scrollbar-color`, so ungating it replaces the prototype's bar. But it
  must key on a **sub-part** pseudo: Firefox reports the bare `::-webkit-scrollbar` as
  _supported_ (a web-compat whitelist — it styles nothing with it), so the original gate applied
  in **no** engine. Note also that `scrollbar-width` is on `*` while `scrollbar-color` is on
  `:root`: only the latter inherits. Both engines now render an 8px bar. Do not "simplify" any
  of this — and **verify scrollbars headed**, since headless renders none at all (every width 0px).

## Accessibility (R-59, spec §5.8 — NFR-A11Y-01..06)

Binding on every component. None of it changes a pixel under mouse interaction.

- **Use native elements.** `<button>` for anything clickable — **a click handler on a `<div>` is
  prohibited** — plus real `<label>`s, landmarks and headings. The prototype is _not_ the
  authority here: it has 110 `<div>`s to 8 `<button>`s and 13 `onClick` handlers, so copying it
  faithfully produces an app a keyboard user cannot operate. Restyle the native element instead.
- **Everything mouse-reachable must be keyboard-reachable**, including hover-revealed
  affordances (rename/delete, the message action bar). Modals trap focus and close on Escape.
- **Use the `--motion-*` tokens; never hard-code a duration, and never write your own
  `prefers-reduced-motion` block.** `tokens.css` handles it globally, and it does so by
  _redefining_ the `dotPulse`/`fadeUp` keyframes rather than switching them off — because
  `animation: none` and the usual `.01ms` blanket both fall back to the element's base style,
  which measured at opacity **0.25** (invisible) for a natural `.dot { opacity: .25 }`. The
  typing dots are a **state indicator**, not decoration.
- **The focus ring is already declared globally** on `:focus-visible` using `--accent`
  (measured 3.92–6.00:1 on every surface, both themes). You get it free. `outline: none` is
  prohibited.
- **Colour is never the sole carrier of information.** The palette fails WCAG AA on 12/28 pairs
  in dark and 18/28 in light — recorded as accepted exceptions in NFR-A11Y-06 — so the numeral
  beside an eval chip and the text label beside a status colour are load-bearing, not
  decorative. **Do not add a new colour pair below 4.5:1 text / 3:1 non-text.**

Note the token-block structure this depends on: theme-_independent_ tokens (radii, layout,
motion, focus ring, eval hues, scrollbar thumb) live in their own bare `:root` block **after**
both theme blocks. That is not tidiness — a media query adds no specificity, so a token declared
in `:root, :root[data-theme='dark']` (0,2,0) can never be overridden at plain `:root`, and the
reduced-motion block was a silent no-op until this was fixed.

Still to come: the FR-HDR-03 segmented toggle that calls `toggleTheme` is **T-504**, and it calls
`useTheme()` **directly** — `AppShell` deliberately takes no `theme` prop (R-58(5)), because that
precedent would drag the other nine FR-CST-01 fields through the shell in T-505..T-508.

## The shell (T-502)

`src/shell/AppShell.tsx` is the FR-LAY-01 three-region layout and nothing else. It takes
**slots** (`sidebar` / `chat` / `stats` / `overlays` as `ReactNode`) rather than importing the
feature components, so the shell knows no feature and no feature knows the shell. `src/App.tsx`
is the composition root: it owns the FR-SYS-04 prop boundary, resolves the `brandName` and
`showStats` defaults, and renders the shell inside `ThemeProvider`. `main.tsx` still renders
`<App />` with **no props** — every FR-SYS-04 default lives in CSS or a destructuring default.

Three things it establishes that later tasks inherit:

- **Landmarks and the heading.** `<nav>` / `<main>` / `<aside>` are the three regions
  (NFR-A11Y-03), and the document's only `<h1>` is here, visually hidden, carrying `brandName`.
  **Every other heading is `<h2>` or deeper** — T-503's "CONVERSATIONS", T-504's chat title,
  T-507's card labels, T-508's modal title. T-509's login screen may own an `h1` because it
  replaces the shell rather than rendering inside it.
- **A skip link** targeting `<main id="corpus-main" tabIndex={-1}>`. Verified headed: first Tab
  reveals it, Enter moves focus to `<main>`, and a _mouse_ click on `<main>` focuses it with no
  ring (`:focus-visible` only), so T-510's screenshots are unaffected.
- **No layout box may set `transform`, `filter`, `backdrop-filter`, `perspective`,
  `will-change` or `contain`.** Each makes the element the containing block for its
  `position: fixed` descendants, which would misplace FR-CIT-03's hover card and make
  FR-KBM-01's `inset: 0` modal cover the chat column instead of the viewport — in a real
  browser only, since jsdom computes no layout. Guarded per block in `AppShell.css.test.ts`.

## The sidebar and the modal primitive (T-503)

`src/sidebar/` holds `Sidebar` (FR-SBR-01/02/05/06), `ConversationList` (FR-SBR-03/04/07) and
the pure helpers in `conversations.ts`. `src/ui/` holds `Dialog` and `ConfirmDialog`.

- **`src/ui/Dialog` is the modal primitive — reuse it, do not re-implement the trap.** It owns
  the whole NFR-A11Y-04 contract: focus trap with wrap at both ends, Escape, focus restore to
  the opener, and the overlay's click-to-close with `stopPropagation` inside. T-508's KB modal
  (FR-KBM-01, 520px) and document-delete confirm (FR-KBM-07), and T-509's change-password
  modal (FR-AUT-09, 420px) are its remaining callers — pass `panelClassName` for the size.
- **Hover-revealed affordances are hidden with `opacity`, never `display`/`visibility`.** That
  is what keeps them in the tab order (NFR-A11Y-04) and out of layout, so the row's default
  appearance is unchanged. Pair it with `:focus-within` on the row, or a keyboard user tabs
  onto an invisible control. The FR-MSG-08 action bar (T-505) has the same shape.
- **A popover inside a scroll container must be `position: fixed`**, positioned from the
  trigger's `getBoundingClientRect()` and closed on scroll/resize — an absolutely-positioned
  one is clipped by the container's `overflow`, which jsdom cannot see. Measured: the
  conversation menu extends 70px past the list's edge. Same for T-506's mention menu if its
  composer ever scrolls.
- **A `<button>` inherits neither `font-size` nor `color`.** Every native element restyled from
  a prototype `<div>` needs `font: inherit; color: inherit`, or it silently computes 13.33px
  and black. Two rows of this shipped before a browser caught it.
- **Watch content-box.** There is no `box-sizing` reset anywhere (the prototype has none), so
  `width: 100%` plus padding overflows, and a declared `width` is not the rendered width.
  `.menu` sets `border-box` deliberately and is the only place that does.

## Component styling: CSS Modules

**One `*.module.css` co-located with each component** — `src/shell/AppShell.module.css` is the
first. Chosen over global stylesheets because eight component tasks will each want `.row`,
`.card`, `.title` and `.label`, and a global collision restyles a component whose author never
opened it — invisible until T-510's pixel pass at the end of the phase. Chosen over inline
`style={{}}` because the prototype's own `style-hover` / `style-focus` attributes are pseudo-
classes, and T-503's hover-revealed affordance needs `:focus-within` to be keyboard-reachable.

- Class names are `camelCase` (`styles.skipLink`); the global utilities in `tokens.css` stay
  kebab-case (`.mono`, `.visually-hidden`).
- Inline styles are for **computed geometry only** — the FR-ANL-03 meter width, the FR-EVL-03
  bar, the FR-CIT-03 hover card's coordinates. That is data, not styling.
- Never a raw colour or a hard-coded duration: use a token, or NFR-A11Y-01's reduced-motion
  baseline cannot reach you. Never add a `prefers-reduced-motion` block of your own.
- **Copy the `AppShell.css.test.ts` guard into each new component.** CSS Modules have one hole:
  `vite/client` types the import as an index signature, and Vitest stubs it with a Proxy that
  answers _any_ key with a truthy string — so `styles.typo` passes `tsc`, passes every render
  test, and ships as `class="undefined"`. The guard cross-references `styles.X` in the TSX
  against `.X` in the stylesheet. Shared helpers live in `src/test/css-source.ts`.
- Note that **Vitest never parses CSS**, so a syntax error in a module reaches only
  `npm run build`. That step is required, not optional.

## Commands

```bash
npm install        # install deps (Node 24)
npm run dev        # Vite dev server (proxies /api + /health to 127.0.0.1:8000)
npm run api:generate  # regenerate src/api/schema.d.ts from ../backend/openapi.json
npm run build      # api:generate && tsc -b && vite build  (must be green)
npm run preview    # preview the production build
npm test           # vitest run (jsdom + @testing-library/react)
npm run test:watch
npm run lint       # oxlint
npm run format     # prettier --write .
npm run format:check
```

The dev server expects the backend at `http://127.0.0.1:8000`:

```bash
cd ../backend && uv run uvicorn app.main:app --loop app.runtime:selector_loop
```

## The generated API client (T-405, R-57)

`src/api/schema.d.ts` is **generated** from `../backend/openapi.json` and committed. Never edit
it, and never hand-write a request or response type — import from `src/api` instead, whose
aliases all resolve into the generated schema.

- **Changed a backend route?** `cd ../backend && uv run python -m app.openapi --write`, then
  `npm run api:generate`. A backend pytest fails if you forget the first; `npm run build` runs
  the second for you, so a stale client cannot reach a build.
- **HTTP** goes through `api` (`openapi-fetch`): `api.GET('/api/v1/conversations', …)` — paths,
  params, bodies and responses are all checked against the schema.
- **SSE** goes through `streamFrames` — `fetch` + `ReadableStream`, **never `EventSource`**,
  which cannot send the `Authorization` header these streams require (R-41(3)). Each frame is
  one JSON envelope `{event, data}`; switch on `frame.event` and the payload narrows.
- Auth (bearer token, silent refresh, 401 handling) is **T-509's** and is deliberately absent.

## Browser support (NFR-CMP-01, R-61)

**Chrome/Edge 111+ · Firefox 114+ · Safari & iOS Safari 16.4+.**

Pinned as `BROWSER_TARGET` in `vite.config.ts` and asserted by `src/test/build-target.test.ts`.
These are the versions Vite's `baseline-widely-available` default already resolved to, so pinning
changed no output — the point is that the matrix stops being a bundler default. While it was one,
a Vite upgrade could drop a browser from support, or emit syntax an in-matrix browser cannot
parse, and nothing would fail.

Changing the matrix is a **requirement change**: amend NFR-CMP-01 and `BROWSER_TARGET` together.
Note the accessibility coupling before widening it — below Safari 15.4 `:focus-visible` stops
parsing and the NFR-A11Y-02 focus ring silently disappears. Leave `build.cssTarget` unset so it
keeps following `build.target`; setting one without the other lets the CSS and JS floors drift.

## Tooling notes

- Linter: **oxlint** (shipped by create-vite v9). Formatter: **Prettier** (`.prettierrc`).
- `.npmrc` sets `legacy-peer-deps=true`: `openapi-typescript` 7.13 still declares
  `peer typescript@"^5.x"` while this project is on TypeScript 6. It is a build-time generator
  that never runs in the app, and `npm run build` proves its output compiles under TS 6.
  Remove the flag once upstream widens the range.
- No image assets or icon/UI libraries — text glyphs + CSS shapes only (NFR-CMP-03).
- Tests: **vitest** + **jsdom** + **@testing-library/react** (T-501). `globals: false`, so import
  `describe`/`it`/`expect` explicitly and note that RTL's auto-cleanup is registered by hand in
  `src/test/setup.ts`. The config lives in `vite.config.ts` via `vitest/config` — one config file,
  no duplicated plugin or proxy setup. Deliberately absent: `jest-dom`, `user-event`, coverage.
  Because `legacy-peer-deps` is on, **check peer ranges by hand** when adding a dev dependency —
  it will happily install a vitest that excludes this project's Vite major.
