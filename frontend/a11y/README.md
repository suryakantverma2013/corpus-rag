# The accessibility pass (T-614)

`npm run a11y` — injects `axe-core` over CDP and runs it against **10 surfaces × 2 themes**.
Exit code is the number of failed checks, exactly as `frontend/fidelity/` does.

```bash
cd frontend
CORPUS_PASSWORD='…' npm run a11y                              # against the dev server
CORPUS_PASSWORD='…' APP_URL=http://localhost:8088/ npm run a11y   # against a built origin
A11Y_HEADLESS=0 CORPUS_PASSWORD='…' npm run a11y              # watch it drive
```

It needs the app **and** the backend up, and signs in as `admin@corpus.local`. The cloud picker
and its unlink confirmation need a **linked Drive account** in that environment; without one those
two surfaces fail rather than skipping, for the reason in "Not reaching a surface is not a pass"
below — but **only those two**, in both themes. Everything else is still measured (T-724).

## Why this exists

`axe-core` had been a devDependency since T-511 with **zero tracked call sites**:
`frontend/ACCESSIBILITY.md` is a write-up of an injection performed by hand, so NFR-A11Y-06's
conformance claim was the one acceptance statement in the product nobody could re-run. §8.64(2)
is the precedent — _a verification instrument that exists only as a written result certifies
rather than verifies_ — and the enumerated exception list is exactly the kind that grows quietly.

## It does not assert zero violations, and must not

NFR-A11Y-06 **accepts** the §5.1 palette's contrast failures as recorded exceptions: 12 of 28
token pairs fail in dark and 18 of 28 in light, and closing them means changing token values,
which is a visual redesign the design handoff has not authorised. A harness asserting zero would
be red on every commit until someone deleted it.

So the assertion is **no violation outside the enumerated set**, which is two things:

1. **No rule other than `color-contrast` and `region`.** That is the WCAG claim this defends.
2. **No new colour pair below threshold** (`ACCESSIBILITY.md` rule 5) — checked on the
   **foreground**, not the (fg, bg) pair. Backgrounds here are composited (§8.63 measured
   `--accent` on `--accent-soft` over `--bubble` at 3.56:1), so pinning pairs would report a new
   pair every time a surface's stacking changed. The foreground is the token the enumeration
   actually names. See `accepted.mjs`.

The accepted foregrounds and the worst ratio seen for each are printed every run, so the
enumeration carries its own evidence rather than living only in prose.

## Two things that are easy to get wrong

**Scope the probe to the panel under test.** Anything modal or popover is measured with
`axe.run(panel)`. §8.63(7) recorded that running unscoped with a modal open makes axe measure
the content _behind the scrim_ and report 1.01:1. **Measured again at axe-core 4.13 and it no
longer reproduces**: obscured nodes now land in `incomplete` ("background color could not be
determined because it partially overlaps"), not in violations, and unscoping the third-dialog
probe changed no result. The scoping stays — it is cheaper, it is what the surface under test
means, and `incomplete` handling is a library detail that can move — but the guard now rests on
that reasoning rather than on a symptom this version still shows.

**Wait for the animation before measuring.** axe reads _computed_ colour, and popovers open
through the global `.animate-fade-up` utility. Sampled mid-fade, a menu item's foreground is
blended into the panel beneath it: the first run of this harness reported `#1d222f` on `#181d2a`
at **1.05:1**, a "new colour pair" that exists for 150 ms and belongs to no token. It fired in
the dark pass only, because dark runs first and was therefore the one that raced. `settle()`
waits on the animation's own `playState`, never a sleep — a harness that is occasionally red is
one people learn to re-run until it is green (§8.64).

## Not reaching a surface is not a pass

T-606 found the fidelity harness recording _"no linked Drive account — not a fidelity failure"_
as a **pass**, which left the browser sitting on Keycloak while 39 later checks measured
PatternFly. Here, a surface that cannot be opened is a **failure to measure**, and nothing is ever
measured off-origin. `probe()` also asserts that axe found a non-empty rule set, so a selector
that stops matching fails instead of reporting a clean surface.

**But refusing to measure must not become refusing to measure _anything_ (T-724).** The first
version of that rule threw on leaving the origin — from the last step of the _dark_ iteration,
which took all 27 light-theme checks with it. The run then reported `29/31`: not wrong, but
quietly incomplete, which is the §8.64 failure one level up. So the cloud-drive surfaces now run
**last and in a loop of their own**, and the browser is **returned to the app and verified** after
a redirect. A missing Drive link costs exactly the two surfaces that depend on it — measured, not
asserted: with the redirect simulated in both themes the run records 2 failures and still passes
51 checks across both themes, against 29 before.

Note what did _not_ change: it is still a failure, it still counts toward the exit code, and the
recovery is a deliberate re-navigate whose success is asserted — never a `catch` that continues
and hopes, which would reproduce T-606's defect one step later.

## What it found on its first run

A real WCAG 2.2 AA defect that `ACCESSIBILITY.md` said did not exist: `target-size` (2.5.8) on
the login screen's password **Show/Hide** button — 39.6 × 19 px, failing both the 24 × 24 minimum
and the spacing exception (19 px safe diameter against 24 px required). Fixed in
`LoginScreen.module.css`; the label does not move, because the button is absolutely positioned
and re-centred by `translateY(-50%)`.

T-511 and T-514 could not have seen it: `target-size` is tagged `wcag22aa`, and their runs did
not include that tag. This harness does.

## Mutation-checked

A green harness that cannot fail certifies rather than verifies, so it was proved to fail first:

| Mutation                                            | Result                                                     |
| --------------------------------------------------- | ---------------------------------------------------------- |
| The real `target-size` defect (before it was fixed) | **caught** — login, both themes                            |
| `Dialog`'s `aria-labelledby` → a missing id         | **caught** — `aria-dialog-name` on 3 surfaces, both themes |
| `--muted` (dark) → a failing, unenumerated colour   | **caught** — new pair `#3a3f4d` at 1.72:1, with CSS paths  |
| Unscoping the picker / unlink probes                | **not caught** — see the scoping note above                |

The first mutation attempted — removing the `aria-label` from the conversation `⋯` trigger — was
**withdrawn as invalid**: the harness selects that button _by_ its label, so it broke the opener
and the run failed as "surface not reachable" rather than on `button-name`. That is §8.65(5)'s
trap (the mutation reached a different mechanism than the one it named), and it is why the
replacement mutation targets a control no selector here depends on.
