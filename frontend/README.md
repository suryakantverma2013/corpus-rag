# Corpus GUI (frontend)

The "Corpus" web UI for the Nexus AI RAG platform. **React + Vite + TypeScript**
(stack locked by spec R-16 / R-26 — Next.js declined). Currently: the T-004 token
scaffold, the T-405 generated API client, and T-501's theme runtime. The three-column
shell and the components follow in T-502..T-509.

## Design authority

The UI is **pixel-perfect** (NFR-VIS-01) to the prototype:

> `../RAG Chatbot GUI Design/design_handoff_rag_chatbot/RAG Chatbot.dc.html`

When instinct and the prototype disagree, the prototype wins. The only exceptions
are the Rev 0.5/0.6 additions where the spec (`../Nexus_AI_Detailed_Specification.md`)
is the baseline. See the `frontend-dev` skill for the full convention list.

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

Still to come: the FR-HDR-03 segmented toggle that calls `toggleTheme` is **T-504**; the
three-column shell and the `showStats` / `brandName` props are **T-502** — which must render
_inside_ `ThemeProvider` and must not thread `theme` through its own props.

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
