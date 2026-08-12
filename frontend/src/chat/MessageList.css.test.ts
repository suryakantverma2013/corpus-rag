/**
 * Structural guards on the §4.3 stylesheets — the `ChatHeader.css.test.ts` pattern, including the
 * CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every rule
 * below is verified in a real browser and formally by T-510. What they defend against is a rule
 * being deleted or "simplified" by someone who does not know why it is shaped that way.
 */
import { describe, expect, it } from 'vitest';
import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['MessageList', 'src/chat/MessageList.module.css', 'src/chat/MessageList.tsx'],
  ['UserMessage', 'src/chat/UserMessage.module.css', 'src/chat/UserMessage.tsx'],
  ['AiMessage', 'src/chat/AiMessage.module.css', 'src/chat/AiMessage.tsx'],
  ['EvalChips', 'src/chat/EvalChips.module.css', 'src/chat/EvalChips.tsx'],
  ['MessageActions', 'src/chat/MessageActions.module.css', 'src/chat/MessageActions.tsx'],
  ['CitationChip', 'src/chat/CitationChip.module.css', 'src/chat/CitationChip.tsx'],
  ['CitationCard', 'src/chat/CitationCard.module.css', 'src/chat/CitationCard.tsx'],
] as const;

describe.each(SHEETS)('%s stylesheet', (_name, cssPath, tsxPath) => {
  const css = readSource(cssPath);
  const code = stripBlockComments(css);
  const tsxCode = stripTsComments(readSource(tsxPath));

  it('declares every class the component reads', () => {
    // The CSS-Modules hole T-502 documented: `vite/client` types the import as an index
    // signature and Vitest stubs it with a Proxy answering any key, so `styles.typo` passes
    // `tsc`, passes every render test, and ships as class="undefined".
    const declared = new Set([...css.matchAll(/^\.([A-Za-z][\w-]*)/gm)].map((m) => m[1]));
    const used = [...tsxCode.matchAll(/\bstyles\.([A-Za-z]\w*)/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(0);
    for (const name of used) {
      expect([...declared], `styles.${name} is not declared in ${cssPath}`).toContain(name);
    }
  });

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    // The baseline is per-animation and lives in tokens.css — including, for the FR-MSG-05 dots,
    // a redefinition of the keyframe itself. A local block here would fall the element back to
    // its resting style, which is precisely the failure that mechanism prevents.
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour, apart from the prototype’s own #fff (NFR-VIS-02)', () => {
    // `#fff` on the accent AI avatar is the prototype's literal and a recorded NFR-A11Y-06
    // exception (3.17:1 in dark); R-58 logged it as a palette decision, not an implementation
    // one. Nothing else may be a raw colour.
    expect(code.replace(/#fff\b/g, '')).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });
});

describe('MessageList — FR-MSG-01 declarations', () => {
  const css = readSource('src/chat/MessageList.module.css');

  it('gives the list the prototype’s scroll box', () => {
    const list = stripBlockComments(blockAfter(css, '\n.list {'));
    expect(list).toMatch(/flex:\s*1/);
    expect(list).toMatch(/overflow-y:\s*auto/);
    expect(list).toMatch(/padding:\s*26px 30px/);
    expect(list).toMatch(/gap:\s*22px/);
    expect(list).toMatch(/flex-direction:\s*column/);
  });

  it('restates no scrollbar styling of its own (NFR-USE-05, R-60)', () => {
    // The 8px bar is global in tokens.css, behind the `::-webkit-scrollbar-thumb` capability
    // gate, with `scrollbar-width` on `*` because it does not inherit. Restating any of it here
    // is how Chrome 121+ silently drops the styled bar for its native one.
    const code = stripBlockComments(css);
    expect(code).not.toContain('scrollbar-thumb');
    expect(code).not.toContain('::-webkit-scrollbar');
    expect(code).not.toContain('scrollbar-width');
    expect(code).not.toContain('scrollbar-color');
  });

  it('scrolls by assignment, never scrollIntoView (FR-MSG-01)', () => {
    // `scrollIntoView` scrolls every scrollable ancestor, which here would move the whole app.
    // The requirement names the mechanism, so it is asserted rather than assumed.
    const code = stripTsComments(readSource('src/chat/MessageList.tsx'));
    expect(code).not.toContain('scrollIntoView');
    expect(code).toContain('scrollTop = element.scrollHeight');
  });
});

describe('MessageList — FR-MSG-02 empty state', () => {
  const css = readSource('src/chat/MessageList.module.css');

  it('centres the block at the prototype’s box', () => {
    const empty = stripBlockComments(blockAfter(css, '\n.empty {'));
    expect(empty).toMatch(/margin:\s*auto/);
    expect(empty).toMatch(/max-width:\s*380px/);
    expect(empty).toMatch(/gap:\s*10px/);
    expect(empty).toMatch(/text-align:\s*center/);

    const mark = stripBlockComments(blockAfter(css, '\n.emptyMark {'));
    expect(mark).toMatch(/width:\s*44px/);
    expect(mark).toMatch(/height:\s*44px/);
    expect(mark).toMatch(/background:\s*var\(--accent-soft\)/);
    expect(mark).toMatch(/font-size:\s*20px/);
  });

  it('resets the UA heading styling the prototype’s <div> never had', () => {
    // An <h3> arrives with a UA margin and its own font-size/weight, where the prototype's
    // element inherits the 14px root font and overrides only size and weight (T-504's finding).
    const title = stripBlockComments(blockAfter(css, '\n.emptyTitle {'));
    expect(title).toMatch(/margin:\s*0/);
    expect(title).toMatch(/font-size:\s*17px/);
    expect(title).toMatch(/font-weight:\s*600/);
    expect(title).toMatch(/color:\s*inherit/);
  });
});

describe('MessageList — FR-MSG-05 typing indicator', () => {
  const css = readSource('src/chat/MessageList.module.css');

  it('applies dotPulse through the GLOBAL utility, never a module-local reference', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe it does not declare, so
    // `animation: dotPulse …` written in this sheet compiles to `_dotPulse_<hash>`, matches no
    // @keyframes rule and silently never runs — measured headed in Chromium during T-505, with
    // the dots sitting still at opacity 1 while every computed value read correctly.
    expect(stripBlockComments(css)).not.toMatch(/animation:\s*dotPulse/);
    expect(stripTsComments(readSource('src/chat/MessageList.tsx'))).toContain('animate-dot-pulse');
    expect(stripBlockComments(blockAfter(css, '\n.dot2 {'))).toMatch(
      /animation-delay:\s*var\(--motion-dot-delay\)/,
    );
    expect(stripBlockComments(blockAfter(css, '\n.dot3 {'))).toMatch(
      /animation-delay:\s*calc\(var\(--motion-dot-delay\) \* 2\)/,
    );
  });

  it('declares no resting opacity on the dot (NFR-A11Y-01)', () => {
    // Measured in Chromium: with `opacity: .25` as the resting style the dots land at 0.25 —
    // invisible — under any treatment that ends the animation. tokens.css defends against that
    // by redefining the keyframe, but not authoring a resting opacity is the belt to its braces.
    expect(stripBlockComments(blockAfter(css, '\n.dot {'))).not.toMatch(/opacity/);
  });

  it('gives the typing avatar NO margin-top, unlike FR-MSG-04’s', () => {
    // The prototype's single difference between the two avatars (line 126 vs line 99). Expressed
    // by SCOPING the margin to the AI row rather than by an override: two single-class rules are
    // a specificity tie decided by stylesheet order, and measured headed the override lost — the
    // typing avatar rendered at 2px with the `margin-top: 0` rule sitting there unapplied.
    const ai = readSource('src/chat/AiMessage.module.css');
    expect(stripBlockComments(blockAfter(ai, '\n.avatar {'))).not.toMatch(/margin-top/);
    expect(stripBlockComments(blockAfter(ai, '\n.row .avatar {'))).toMatch(/margin-top:\s*2px/);
    expect(stripBlockComments(css)).not.toContain('typingAvatar');
  });

  it('gives the bubble the prototype’s box', () => {
    const bubble = stripBlockComments(blockAfter(css, '\n.typingBubble {'));
    expect(bubble).toMatch(/background:\s*var\(--panel\)/);
    expect(bubble).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(bubble).toMatch(/border-radius:\s*var\(--radius-bubble\)/);
    expect(bubble).toMatch(/padding:\s*12px 16px/);
    expect(bubble).toMatch(/gap:\s*5px/);
  });
});

describe('MessageList — FR-STA-04 frozen conversation (R-67)', () => {
  const css = readSource('src/chat/MessageList.module.css');

  it('renders as a quiet notice, never as an error', () => {
    // R-67(1): frozen, not broken — a designed terminal state. Using the FR-EVL-03 bad hue here
    // would say "something went wrong" about a conversation that is working perfectly.
    const frozen = stripBlockComments(blockAfter(css, '\n.frozen {'));
    expect(frozen).toMatch(/background:\s*var\(--panel2\)/);
    expect(frozen).toMatch(/color:\s*var\(--muted\)/);
    expect(frozen).not.toContain('--eval-bad');
  });

  it('names no number in its copy (R-51(5))', () => {
    // The limit is a `# TBD(§8.4)` knob and a user can act on "start a new chat" but not on
    // "10,400" — so the copy must never acquire one.
    const code = stripTsComments(readSource('src/chat/MessageList.tsx'));
    const notice = code.slice(
      code.indexOf('const FROZEN_NOTICE'),
      code.indexOf('export interface'),
    );
    expect(notice).not.toMatch(/\d/);
  });
});

describe('UserMessage — FR-MSG-03 declarations', () => {
  const css = readSource('src/chat/UserMessage.module.css');

  it('gives the bubble the prototype’s box, radius token and all', () => {
    const bubble = stripBlockComments(blockAfter(css, '\n.bubble {'));
    expect(bubble).toMatch(/max-width:\s*62%/);
    expect(bubble).toMatch(/background:\s*var\(--bubble\)/);
    expect(bubble).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(bubble).toMatch(/border-radius:\s*var\(--radius-bubble-user\)/);
    expect(bubble).toMatch(/padding:\s*11px 15px/);
    expect(bubble).toMatch(/font-size:\s*13.5px/);
    expect(bubble).toMatch(/line-height:\s*1.55/);
    // The asymmetric radius is §5.1's own token; repeating the literal here is how the two drift.
    expect(bubble).not.toMatch(/14px 14px 4px 14px/);
  });

  it('right-aligns the row and animates it in', () => {
    const row = stripBlockComments(blockAfter(css, '\n.row {'));
    expect(row).toMatch(/justify-content:\s*flex-end/);
    expect(row).toMatch(/gap:\s*12px/);
    // `fadeUp` is applied by the global `.animate-fade-up` utility, never referenced from this
    // module — see the dot guard above for the mechanism, and tokens.css for the note.
    expect(row).not.toMatch(/animation:/);
    expect(stripTsComments(readSource('src/chat/UserMessage.tsx'))).toContain('animate-fade-up');
  });

  it('gives the avatar the prototype’s round 26px', () => {
    const avatar = stripBlockComments(blockAfter(css, '\n.avatar {'));
    expect(avatar).toMatch(/width:\s*26px/);
    expect(avatar).toMatch(/border-radius:\s*var\(--radius-avatar\)/);
    expect(avatar).toMatch(/background:\s*var\(--panel2\)/);
    expect(avatar).toMatch(/font-size:\s*10.5px/);
    expect(avatar).toMatch(/flex-shrink:\s*0/);
  });
});

describe('AiMessage — FR-MSG-04 and FR-MSG-07 declarations', () => {
  const css = readSource('src/chat/AiMessage.module.css');

  it('gives the content column the prototype’s box AND lets it shrink', () => {
    const content = stripBlockComments(blockAfter(css, '\n.content {'));
    expect(content).toMatch(/max-width:\s*68%/);
    expect(content).toMatch(/gap:\s*8px/);
    // Without `min-width: 0` a wide code block or table sets the flex item's automatic minimum
    // size and pushes the column past 68% instead of scrolling inside it (T-504's mechanism).
    expect(content).toMatch(/min-width:\s*0/);
  });

  it('gives the body the prototype’s type', () => {
    const body = stripBlockComments(blockAfter(css, '\n.body {'));
    expect(body).toMatch(/font-size:\s*13.5px/);
    expect(body).toMatch(/line-height:\s*1.65/);
  });

  it('gives the AI avatar the accent square', () => {
    const avatar = stripBlockComments(blockAfter(css, '\n.avatar {'));
    expect(avatar).toMatch(/border-radius:\s*var\(--radius-btn\)/);
    expect(avatar).toMatch(/background:\s*var\(--accent\)/);
    expect(avatar).toMatch(/font-weight:\s*700/);
    expect(avatar).toMatch(/font-size:\s*12px/);
  });

  it('mono-sizes the source line at the prototype’s 11px muted2', () => {
    const line = stripBlockComments(blockAfter(css, '\n.sourceLine {'));
    expect(line).toMatch(/font-size:\s*11px/);
    expect(line).toMatch(/color:\s*var\(--muted2\)/);
  });

  it('scrolls wide code and tables horizontally rather than widening the column', () => {
    // FR-MSG-07 names the behaviour for code blocks; a wide table has the same problem and the
    // same answer.
    expect(stripBlockComments(blockAfter(css, '\n.pre {'))).toMatch(/overflow-x:\s*auto/);
    expect(stripBlockComments(blockAfter(css, '\n.tableWrap {'))).toMatch(/overflow-x:\s*auto/);
    // A fence is verbatim text and must not be re-wrapped by the body's overflow-wrap.
    expect(stripBlockComments(blockAfter(css, '\n.pre {'))).toMatch(/white-space:\s*pre/);
  });

  it('reveals the FR-MSG-08 bar by opacity, never by removing it (NFR-A11Y-04)', () => {
    // `display: none` / `visibility: hidden` look identical to a mouse user and take all three
    // buttons out of the tab order — the exact defect T-503 fixed for FR-SBR-07's affordance.
    const code = stripBlockComments(css);
    expect(stripBlockComments(blockAfter(css, '\n.actions {'))).toMatch(/opacity:\s*0/);
    expect(code).toMatch(/\.row:hover \.actions/);
    expect(code).toMatch(/\.actions:focus-within/);
    expect(code).not.toMatch(/display:\s*none/);
    expect(code).not.toMatch(/visibility:\s*hidden/);
  });
});

describe('EvalChips — FR-EVL-02/03 declarations', () => {
  const css = readSource('src/chat/EvalChips.module.css');

  it('gives the row and chip the prototype’s box', () => {
    const row = stripBlockComments(blockAfter(css, '\n.row {'));
    expect(row).toMatch(/flex-wrap:\s*wrap/);
    expect(row).toMatch(/gap:\s*6px/);

    const chip = stripBlockComments(blockAfter(css, '\n.chip {'));
    expect(chip).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(chip).toMatch(/padding:\s*2px 8px/);
    expect(chip).toMatch(/font-size:\s*10.5px/);
    expect(chip).toMatch(/color:\s*var\(--muted\)/);
    expect(chip).toMatch(/gap:\s*5px/);
  });

  it('colours each dot from its FR-EVL-03 token', () => {
    expect(stripBlockComments(blockAfter(css, '\n.dotGood {'))).toMatch(/var\(--eval-good\)/);
    expect(stripBlockComments(blockAfter(css, '\n.dotWarn {'))).toMatch(/var\(--eval-warn\)/);
    expect(stripBlockComments(blockAfter(css, '\n.dotBad {'))).toMatch(/var\(--eval-bad\)/);
  });

  it('keeps the numeral beside the dot (NFR-A11Y-06)', () => {
    // The FR-EVL-03 hues all fail contrast as text in the light theme, and NFR-A11Y-06 accepts
    // that only on the condition that colour is never the sole carrier. The number is the
    // carrier, so rendering the dot alone would breach the exception the palette relies on.
    const code = stripTsComments(readSource('src/chat/EvalChips.tsx'));
    expect(code).toContain('${chip.label} ${chip.score}');
  });
});

describe('CitationChip — FR-CIT-01 declarations', () => {
  const css = readSource('src/chat/CitationChip.module.css');
  const chip = stripBlockComments(blockAfter(css, '\n.chip {'));

  it('reproduces the prototype’s ten declarations', () => {
    expect(chip).toMatch(/display:\s*inline-flex/);
    expect(chip).toMatch(/background:\s*var\(--accent-soft\)/);
    expect(chip).toMatch(/color:\s*var\(--accent\)/);
    expect(chip).toMatch(/border-radius:\s*5px/);
    expect(chip).toMatch(/padding:\s*1px 7px/);
    expect(chip).toMatch(/font-size:\s*11.5px/);
    expect(chip).toMatch(/font-weight:\s*600/);
    expect(chip).toMatch(/cursor:\s*pointer/);
    expect(chip).toMatch(/margin:\s*0 2px/);
    expect(chip).toMatch(/border:\s*1px solid transparent/);
    // Nudges the chip onto the surrounding 1.65 line box's baseline — reads as noise and is not.
    expect(chip).toMatch(/vertical-align:\s*1px/);
    expect(stripBlockComments(css)).toMatch(
      /\.chip:hover\s*\{[^}]*border-color:\s*var\(--accent\)/,
    );
  });

  it('does not tokenise its radius to --radius-chip', () => {
    // That token is 4px; FR-CIT-01 and FR-EVL-02 are the "5px" half of its own "4–5px" comment.
    // Tokenising would silently render a pixel tighter in two places at once.
    expect(chip).not.toContain('--radius-chip');
    expect(stripBlockComments(readSource('src/chat/EvalChips.module.css'))).not.toContain(
      '--radius-chip',
    );
  });

  it('declares the font properties a <button> does not inherit (T-503)', () => {
    // Measured at 13.33px/black against the prototype's own value. `font-family` comes from the
    // global `.mono` utility on the same element, which is why it is not declared here.
    expect(chip).toMatch(/font-size/);
    expect(chip).toMatch(/font-weight/);
    expect(chip).toMatch(/color/);
    expect(chip).toMatch(/line-height:\s*inherit/);
    expect(stripTsComments(readSource('src/chat/CitationChip.tsx'))).toContain('mono');
  });

  it('is a real <button>, not the prototype’s <span> (NFR-A11Y-03/04)', () => {
    const code = stripTsComments(readSource('src/chat/CitationChip.tsx'));
    expect(code).toContain('<button');
    expect(code).toContain('type="button"');
    // FR-CIT-02's mouse contract, plus the keyboard path the prototype has no equivalent for.
    for (const handler of ['onMouseEnter', 'onMouseLeave', 'onFocus', 'onBlur']) {
      expect(code).toContain(handler);
    }
  });
});

describe('CitationCard — FR-CIT-03/04 declarations', () => {
  const css = readSource('src/chat/CitationCard.module.css');
  const card = stripBlockComments(blockAfter(css, '\n.card {'));

  it('reproduces the prototype’s popover box', () => {
    expect(card).toMatch(/position:\s*fixed/);
    // `translate`, NOT `transform`: the entrance animation sets `transform`, and an animated
    // transform REPLACES the element's own — measured headed, the card sat 146px low for the
    // animation's duration and snapped up at the end. `translate` composes instead.
    expect(card).toMatch(/translate:\s*0 calc\(-100% - 10px\)/);
    expect(card).not.toMatch(/transform:/);
    expect(card).toMatch(/background:\s*var\(--panel\)/);
    expect(card).toMatch(/border-radius:\s*var\(--radius-pop\)/);
    expect(card).toMatch(/box-shadow:\s*var\(--shadow\)/);
    expect(card).toMatch(/padding:\s*14px 16px/);
    // The entrance is the global utility's; this sheet supplies only its duration.
    expect(card).not.toMatch(/animation:/);
    expect(card).toMatch(/--fade-up-duration:\s*var\(--motion-fast\)/);
    expect(stripTsComments(readSource('src/chat/CitationCard.tsx'))).toContain('animate-fade-up');
    // FR-CIT-02: the card is non-interactive.
    expect(card).toMatch(/pointer-events:\s*none/);
  });

  it('reads the stacking order from the token, not the literal 60', () => {
    expect(card).toMatch(/z-index:\s*var\(--z-hovercard\)/);
    expect(card).not.toMatch(/z-index:\s*60/);
  });

  it('keeps the card width and the clamp constant in step', () => {
    // The clamp is `innerWidth - (width + 20)`, computed in the provider from CARD_CLAMP. If the
    // stylesheet's width moves and the constant does not, the card silently runs off-screen.
    const width = /width:\s*(\d+)px/.exec(card);
    expect(width).not.toBeNull();
    const ts = stripTsComments(readSource('src/chat/CitationHoverProvider.tsx'));
    expect(ts).toContain(`CARD_WIDTH = ${width![1]}`);
    expect(ts).toContain(`CARD_CLAMP = ${Number(width![1]) + 20}`);
  });

  it('renders the quote block with the prototype’s accent rule', () => {
    const quote = stripBlockComments(blockAfter(css, '\n.quote {'));
    expect(quote).toMatch(/font-size:\s*12.5px/);
    expect(quote).toMatch(/line-height:\s*1.6/);
    expect(quote).toMatch(/color:\s*var\(--muted\)/);
    expect(quote).toMatch(/border-left:\s*2px solid var\(--accent\)/);
    expect(quote).toMatch(/padding-left:\s*11px/);
  });

  it('never parses a locator label (FR-CIT-04, R-34)', () => {
    // Clients read the published fields; only PDF has pages and nothing is synthesised for the
    // other formats, so any arithmetic on the label here would be reconstructing what the
    // backend deliberately publishes.
    const code = stripTsComments(readSource('src/chat/CitationCard.tsx'));
    expect(code).not.toMatch(/parseInt|parseFloat|Number\(/);
    expect(code).not.toMatch(/\.split\(|\.replace\(|\.match\(/);
  });
});

describe('FR-MSG-07 sanitization is structural, not a library call', () => {
  it('never reaches for innerHTML anywhere in src/chat', () => {
    // The whole basis of the requirement's "no raw HTML/script execution": there is no code path
    // from a `<` in the input to an element, because no markup string is ever built.
    for (const [, , tsxPath] of SHEETS) {
      const code = stripTsComments(readSource(tsxPath));
      expect(code, tsxPath).not.toContain('dangerouslySetInnerHTML');
      expect(code, tsxPath).not.toContain('innerHTML');
    }
    for (const path of ['src/chat/markdown.ts', 'src/chat/messages.ts']) {
      const code = stripTsComments(readSource(path));
      expect(code, path).not.toContain('dangerouslySetInnerHTML');
      expect(code, path).not.toContain('innerHTML');
      expect(code, path).not.toContain('DOMParser');
    }
  });
});

describe('FR-ERR-04 copy stays server-side (R-43(6))', () => {
  it('names no failure class and reads no error_code in the transcript components', () => {
    // The five classes and their per-class copy live in `app/rag/errors.py`, all `# TBD(§8.4)`.
    // A client-side map is how the two drift — the failure text arrives inside the message frame
    // and is rendered verbatim.
    const classes = [
      'LLM_ERROR',
      'RETRIEVAL_UNAVAILABLE',
      'RATE_LIMITED',
      'TIMEOUT',
      'SYSTEM_FAILURE',
      'error_code',
    ];
    for (const path of [
      'src/chat/AiMessage.tsx',
      'src/chat/MessageList.tsx',
      'src/chat/messages.ts',
    ]) {
      const code = stripTsComments(readSource(path));
      for (const name of classes) {
        expect(code, `${path} names ${name}`).not.toContain(name);
      }
    }
  });
});
