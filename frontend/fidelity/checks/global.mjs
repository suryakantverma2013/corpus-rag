/**
 * §9's global literals: the palette, the fonts, the focus ring, motion and the scrollbar.
 *
 * These are the rows of the acceptance table that belong to no single component, which is why no
 * component task owned them end to end.
 */
import { computed, sleep, token } from '../harness.mjs';

/** §9 "Brand name (default) / props" + the R-58(2)(b) deviation. */
const ACCENT = { dark: '#7c86f8', light: '#5b66e8' };
/** §9 "Accent-soft alpha (R-58(2))". */
const ACCENT_SOFT_ALPHA = { dark: '14%', light: '10%' };
/** §9 "Eval status colors" — identical in both themes (T-507 measured this). */
const EVAL = { '--eval-good': '#4ec3a6', '--eval-warn': '#e8a34c', '--eval-bad': '#e86a8a' };

export async function checkTokens(page, r, theme) {
  r.context('§9 palette + tokens', theme);

  r.eq('--accent', await page.evaluate(token('--accent')), ACCENT[theme]);
  r.eq(
    '--accent-soft-alpha',
    await page.evaluate(token('--accent-soft-alpha')),
    ACCENT_SOFT_ALPHA[theme],
  );

  // R-58(2)(c): `--accent-soft` stays a LITERAL rgba while the `accent` prop is unset, because a
  // permanent `color-mix` derivation is arithmetically exact and still renders 1/255 off. The
  // literal is what must be here — a `color-mix(...)` token stream would mean the derivation
  // became unconditional.
  const soft = await page.evaluate(token('--accent-soft'));
  r.truthy(
    '--accent-soft is a literal rgba, not a color-mix stream',
    /^rgba\(/.test(soft),
    `got ${soft}`,
  );
  const alpha = theme === 'dark' ? '0.14' : '0.1';
  r.truthy(`--accent-soft carries the ${alpha} alpha`, soft.includes(alpha), `got ${soft}`);

  for (const [name, value] of Object.entries(EVAL))
    r.eq(name, await page.evaluate(token(name)), value);

  // §9 "Scrollbar" — the thumb colour is one value for both themes.
  r.eq(
    '--scrollbar-thumb',
    await page.evaluate(token('--scrollbar-thumb')),
    'rgba(128, 136, 160, 0.25)',
  );

  // §9 "Focus ring (NFR-A11Y-02, R-59)".
  r.eq('--focus-ring-width', await page.evaluate(token('--focus-ring-width')), '2px');
  r.eq('--focus-ring-offset', await page.evaluate(token('--focus-ring-offset')), '2px');
}

export async function checkFonts(page, r, theme) {
  r.context('§9 fonts', theme);

  const body = await page.evaluate(`return getComputedStyle(document.body).fontFamily;`);
  r.truthy('body renders Instrument Sans', body.includes('Instrument Sans'), `got ${body}`);

  const mono = await page.evaluate(computed('.mono', ['font-family']));
  r.truthy(
    '.mono renders JetBrains Mono',
    (mono?.['font-family'] ?? '').includes('JetBrains Mono'),
    `got ${mono?.['font-family']}`,
  );

  /*
   * A computed `font-family` only says what was *asked for*. Google Fonts splits each family into
   * unicode-range subsets, so most `FontFace` entries legitimately read `unloaded` for ever — which
   * makes the status list useless as evidence. Two things settle it: the face actually loads when
   * asked, and the rendered width differs from the generic fallback. Without the second check a
   * silent fallback to the platform monospace passes.
   */
  const probe = await page.evaluate(`
    await document.fonts.load('400 32px "JetBrains Mono"', 'RAG0123');
    await document.fonts.load('400 32px "Instrument Sans"', 'RAG0123');
    await document.fonts.ready;
    const faces = [...document.fonts];
    const measure = (family) => {
      const s = document.createElement('span');
      s.textContent = 'MMMMiiii0123 gpt-4o';
      s.style.cssText = 'position:absolute;visibility:hidden;font-size:32px;white-space:pre;font-family:' + family;
      document.body.appendChild(s);
      const w = s.getBoundingClientRect().width;
      s.remove();
      return Math.round(w * 100) / 100;
    };
    return {
      monoLoaded: faces.some(f => f.family.includes('JetBrains') && f.status === 'loaded'),
      sansLoaded: faces.some(f => f.family.includes('Instrument') && f.status === 'loaded'),
      widthMono: measure('"JetBrains Mono"'),
      widthFallback: measure('"NoSuchFace12345", monospace'),
    };`);
  r.truthy('a JetBrains Mono face actually loads', probe.monoLoaded);
  r.truthy('an Instrument Sans face actually loads', probe.sansLoaded);
  r.truthy(
    'JetBrains Mono renders rather than falling back to the platform monospace',
    Math.abs(probe.widthMono - probe.widthFallback) > 1,
    `JetBrains ${probe.widthMono}px vs generic fallback ${probe.widthFallback}px`,
  );
}

export async function checkMotion(page, r, theme) {
  r.context('§9 reduced motion', theme);

  const durations = ['--motion-fast', '--motion', '--motion-slow', '--motion-bar'];
  const normal = {};
  for (const d of durations) normal[d] = await page.evaluate(token(d));
  r.truthy(
    'motion durations are non-zero by default',
    Object.values(normal).every((v) => v !== '0s'),
    JSON.stringify(normal),
  );

  await page.send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
  });
  await sleep(200);
  for (const d of durations) r.eq(`${d} under reduced motion`, await page.evaluate(token(d)), '0s');

  // R-59(2): `--motion-dot` is deliberately NOT zeroed — `dotPulse` is a state indicator, so it is
  // held visible at opacity 1 rather than switched off, which the keyframe redefinition achieves.
  const dot = await page.evaluate(token('--motion-dot'));
  r.truthy('--motion-dot is deliberately not zeroed (R-59(2))', dot !== '0s', `got ${dot}`);

  await page.send('Emulation.setEmulatedMedia', { features: [] });
  await sleep(200);
}

/**
 * §9 "Scrollbar" — 8px, measured under real overflow.
 *
 * R-60(4): headless renders no scrollbars at all, so a 0px reading is inconclusive rather than a
 * pass. This check therefore reports the measured width and fails on 0.
 */
export async function checkScrollbars(page, r, theme) {
  r.context('§9 scrollbar width', theme);

  const widths = await page.evaluate(`
    const out = [];
    const containers = [...document.querySelectorAll('nav ul, main > div, aside')];
    for (const el of containers) {
      const before = el.style.cssText;
      el.style.overflowY = 'scroll';
      const borders = parseFloat(getComputedStyle(el).borderLeftWidth) + parseFloat(getComputedStyle(el).borderRightWidth);
      const w = el.offsetWidth - el.clientWidth - borders;
      el.style.cssText = before;
      out.push({ tag: el.tagName + '.' + String(el.className).slice(0, 18), w: Math.round(w * 100) / 100 });
    }
    return out;`);

  const measured = widths.filter((x) => x.w > 0);
  r.truthy(
    'at least one real scroll container reports a scrollbar (0px everywhere means headless)',
    measured.length > 0,
    JSON.stringify(widths),
  );
  for (const m of measured) r.near(`scrollbar is 8px on ${m.tag}`, m.w, 8, 0.6);
}
