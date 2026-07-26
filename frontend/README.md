# Corpus GUI (frontend)

The "Corpus" web UI for the Nexus AI RAG platform. **React + Vite + TypeScript**
(stack locked by spec R-16 / R-26 — Next.js declined). This is the T-004 scaffold:
tokens + clean build only; the shell and components come in Phase 5 (T-501+).

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

The runtime theme toggle and `accent` prop override are **T-501**; the three-column
shell and `showStats` / `brandName` props are **T-502**.

## Commands

```bash
npm install        # install deps (Node 24)
npm run dev        # Vite dev server
npm run build      # tsc -b && vite build  (must be green — Phase-0 exit criterion)
npm run preview    # preview the production build
npm run lint       # oxlint
npm run format     # prettier --write .
npm run format:check
```

## Tooling notes

- Linter: **oxlint** (shipped by create-vite v9). Formatter: **Prettier** (`.prettierrc`).
- API request/response types will be **generated** from the backend OpenAPI schema
  (`openapi-typescript` / `orval`) — never hand-write them (T-405 pipeline).
- Live updates and token streaming use **SSE** (`EventSource` / fetch stream reader);
  the backend provides no WebSocket channel.
- No image assets or icon/UI libraries — text glyphs + CSS shapes only (NFR-CMP-03).
