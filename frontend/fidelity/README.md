# Visual fidelity harness (T-510)

Verifies NFR-VIS-01..05 and the spec's §9 acceptance-literal table against the **running** product,
in **both themes**, in a **headed** browser.

```bash
# with the backend on :8000 and `npm run dev` on :5173
cd frontend
CORPUS_PASSWORD='<KEYCLOAK_LIVE_ADMIN_PASSWORD from backend/.env>' npm run fidelity
```

Exit code is the number of failed checks. Screenshots land in `fidelity/screenshots/` (gitignored).

## Why it is not a screenshot diff against the prototype

**The prototype cannot render** (R-66, spec §8.49). `RAG Chatbot.dc.html` loads `./support.js`,
which was never shipped in the design handoff, so its template runtime never executes: every
`{{ binding }}` shows as literal text, all 18 `<sc-for>` blocks stay unexpanded, and all 36
`<sc-if>` regions — the knowledge-base modal, the citation hover card, the mention menu — never
appear at all.

What survives is the whole of the baseline: its inline `style` declarations are complete and
readable from source, _including_ on the regions that never paint. So fidelity is verified as
**computed values against those declarations and against §9's literals** — which is what every
component task from T-502 to T-514 did for its own surface. This harness is the end-to-end pass,
and it also owns the §9 rows that belong to no single component (the palette, the fonts, the focus
ring, motion, the scrollbar).

That instrument is also simply better, and would have been the right choice regardless: a
cross-comparison screenshot diff is sensitive to font-load timing and subpixel antialiasing, and
blurs precisely the 264-vs-266px error a computed assertion states outright. Screenshots here are
diagnostics for a human, never a baseline.

## Why it is not part of `npm test`

It needs a headed browser, a dev server and a live backend with a real Keycloak session. `vitest`
runs in jsdom, which applies no external CSS and paints nothing — every measurement here would be
`0px` or `''`. This is the same split the backend uses for its live tests.

**Headed is a measured constraint, not a preference** (R-60(4)): headless Chromium and Firefox
render no scrollbars at all — every width reads `0px`, including an element forced to
`scrollbar-width: auto` — and headless Chromium never matches `:focus`, because the page has no
window focus. `FIDELITY_HEADLESS=1` exists for a smoke run and says so in its banner; a `0px`
scrollbar reading is inconclusive, never a pass.

## Layout: the box-model point that has caught four tasks

§9's figures are the prototype's declared **content** widths, and the prototype ships no
`box-sizing` reset — so a rendered outer width is the declared width plus border and padding.
Both numbers are asserted, and named as which:

| Surface               | Declared | Measured outer                   | Found by      |
| --------------------- | -------- | -------------------------------- | ------------- |
| Sidebar               | 264px    | 265 (+1px border)                | T-502         |
| Stats panel           | 272px    | 309 (+18×2 padding, +1 border)   | T-502 / T-507 |
| FR-KBM-01 modal       | 520px    | 566 (+22×2 padding, +1×2 border) | T-508         |
| §4.17 login card      | —        | 380 (**`border-box`**)           | T-509         |
| Change-password panel | —        | 420 (**`border-box`**)           | T-509         |

The last two are `border-box` because the auth addendum declares it — the opposite convention to
FR-KBM-01's, which is exactly why both numbers are pinned rather than one derived from the other.
A `border-box` reset would silently narrow every panel in the first three rows while a naive
outer-width check kept passing.

## The corpus this expects

The harness signs in and drives a real deployment, so a handful of checks assert things that only
exist once the corpus contains them. That is deliberate: `checkCitationCard` fails with *"no chip
on this conversation"* rather than passing, because a check that quietly skips is a check that
certifies. Prepare, once:

- **at least one answered conversation with a citation** — `selectAnsweredChat()` finds it, and the
  FR-CIT-03 card is opened from its chip.
- **one figure-bearing PDF, with extraction on** — FR-CIT-07 (`checkCitationFigure`). Figure
  extraction ships **off** (`PARSER_FIGURES_ENABLED`, R-94(7)), and only PDFs have pages, so a
  fresh deployment has no figure anywhere and this check is red until one exists:

  ```bash
  # backend/.env, then restart BOTH the API and the arq worker
  PARSER_FIGURES_ENABLED=true
  ```

  Then upload a PDF whose pages carry diagrams and ask a question that cites one of those pages.
  A textbook page works; Markdown, DOCX and CSV never will, because they have no pages for a
  locator to name. **Leave the flag off in `backend/.env` when you are done** — `tests/conftest.py`
  loads that file, so several backend tests that assert the shipped default fail while it is on.

## Writing a new check

- Assert the **measured** value beside the expected one — `r.eq` / `r.near` both report what they
  saw. Three separate tasks (T-506, T-507, T-508) found their _probe_ was wrong rather than the
  code, and the measured value in the log is what makes that visible in seconds.
- **Reach the state you mean to assert.** The first version of this harness passed, then failed
  eight checks on an unchanged build, because the previous run had ended by creating an empty
  conversation and the FR-ANL-04 card shows its empty state there. `selectAnsweredChat()` exists
  for that. An assertion whose truth depends on the order of earlier checks is not a fidelity check.
- **Scope a probe to the element under test.** A document-wide `[class*="_bar_"]` matched the
  _sidebar's_ module class during T-506 and reported six defects in a correct composer; a
  `:last-child` descendant selector matched the wrong element during T-507. CSS Modules' hashed
  names make a substring selector a lottery.
- Prefer roles and accessible names over class names — they are the contract, and they survive a
  CSS-Modules rebuild.

## What it does not cover

- **`--line` borders at 1.18–1.23:1.** WCAG 1.4.11 non-text contrast is not automatable; that group
  is verified by R-59's original measurement. Contrast generally is `frontend/ACCESSIBILITY.md`'s
  (T-511/T-514), not this harness's.
- **Cross-engine rendering.** Firefox is covered by T-511's pass; this runs in Chromium.
- **The FR-KBM-10 cloud picker without a linked Drive account.** It reports the environment fact
  rather than failing, since an unlinked account is not a fidelity defect.

## Run it against the dev server, not a built origin

`APP_URL` defaults to the Vite dev server for a reason that only shows up if you point it
elsewhere: Vite's CSS minifier rewrites `rgba(...)` to 8-digit hex in the built bundle, so
against `http://localhost:8088/` the four `rgba()`-authored tokens report
`--accent-soft: #7c86f824` and `--scrollbar-thumb: #8088a040`. Those are the _same colours_ —
`0x24/255 = 0.141` — but `checkTokens` compares the custom property's literal token stream, which
only survives unminified. It costs **6 false failures** (2 tokens x 2 themes, `--accent-soft`
carrying two assertions each) and nothing else: every geometry, copy and layout check is
origin-independent and passes either way. Measured during T-614.
