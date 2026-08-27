/**
 * FR-MSG-07, asserted at the **AST**, not the DOM.
 *
 * Deliberate: the AST is plain JSON, so these tests are exact and readable, they need no React
 * and no jsdom, and they pin the two properties that matter — where a chip lands, and that no
 * construct can ever produce markup. The `createElement` mapper is thin enough that
 * `AiMessage.test.tsx` covers it end to end.
 */
import { describe, expect, it } from 'vitest';
import type { CitationSegment, Segment } from '../api';
import { citeIndices, flatten, parseMarkdown, renderMarkdown } from './markdown';
import type { Block, Inline } from './markdown';

function cite(doc = 'Q3.pdf'): CitationSegment {
  return { isCite: true, doc, quote: 'q', chunkId: 'c' };
}

/** The whole pipeline up to rendering. */
function parse(segments: Segment[]): Block[] {
  const { source, offsets } = flatten(segments);
  return parseMarkdown(source, offsets);
}

function text(segments: Segment[]): Block[] {
  return parse(segments);
}

/** Flatten an inline tree to `kind` names, for terse structural assertions. */
function kinds(nodes: readonly Inline[]): string[] {
  return nodes.map((n) => n.kind);
}

describe('flatten', () => {
  it('concatenates only the text runs and records citation offsets', () => {
    const { source, citations, offsets } = flatten([
      { text: 'see ' },
      cite('a.pdf'),
      { text: ' and ' },
      cite('b.pdf'),
    ]);
    expect(source).toBe('see  and ');
    expect(citations.map((c) => c.doc)).toEqual(['a.pdf', 'b.pdf']);
    expect(offsets).toEqual([4, 9]);
  });
});

describe('citation placement', () => {
  it('splits a paragraph’s inlines around a mid-sentence chip, staying ONE block', () => {
    const blocks = text([{ text: 'growth was strong ' }, cite(), { text: ' this quarter.' }]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].kind).toBe('para');
    expect(kinds((blocks[0] as Extract<Block, { kind: 'para' }>).children)).toEqual([
      'text',
      'cite',
      'text',
    ]);
  });

  it('keeps a list that STRADDLES a segment boundary as one list', () => {
    // The reason offsets exist. Parsing each `segs` entry independently would produce two lists
    // here — the exact defect FR-MSG-07's "preserved within the rendered flow" is about.
    const blocks = text([{ text: '- alpha\n- be' }, cite(), { text: 'ta\n- gamma' }]);
    expect(blocks).toHaveLength(1);
    const list = blocks[0] as Extract<Block, { kind: 'list' }>;
    expect(list.kind).toBe('list');
    expect(list.items).toHaveLength(3);
    expect(kinds(list.items[1].children)).toEqual(['text', 'cite', 'text']);
  });

  it('places a chip INSIDE the emphasis run it fell in', () => {
    const blocks = text([{ text: 'the **key fi' }, cite(), { text: 'nding** holds.' }]);
    const para = blocks[0] as Extract<Block, { kind: 'para' }>;
    const strong = para.children.find((n) => n.kind === 'strong');
    expect(strong).toBeDefined();
    expect(kinds((strong as Extract<Inline, { kind: 'strong' }>).children)).toEqual([
      'text',
      'cite',
      'text',
    ]);
  });

  it('attaches a trailing chip to the last block — the commonest shape', () => {
    const blocks = text([{ text: 'Revenue rose.' }, cite()]);
    expect(blocks).toHaveLength(1);
    expect(kinds((blocks[0] as Extract<Block, { kind: 'para' }>).children)).toEqual([
      'text',
      'cite',
    ]);
  });

  it('attaches a chip between two paragraphs to the PRECEDING one', () => {
    // A citation supports the claim it follows, not the one that follows it.
    const blocks = text([{ text: 'First claim.\n\n' }, cite(), { text: 'Second claim.' }]);
    expect(blocks).toHaveLength(2);
    expect(kinds((blocks[0] as Extract<Block, { kind: 'para' }>).children)).toEqual([
      'text',
      'cite',
    ]);
    expect(kinds((blocks[1] as Extract<Block, { kind: 'para' }>).children)).toEqual(['text']);
  });

  it('emits a chip that landed inside a fence AFTER the block', () => {
    // `inline-flex` inside <pre> is unstylable, so the chip follows the block instead.
    const blocks = text([{ text: '```\nconst a = 1;\n' }, cite(), { text: '\n```' }]);
    expect(blocks.map((b) => b.kind)).toEqual(['code', 'cites']);
    expect((blocks[1] as Extract<Block, { kind: 'cites' }>).indices).toEqual([0]);
  });

  it('renders a citation-only answer as a chip block rather than dropping it', () => {
    const blocks = text([cite()]);
    expect(blocks.map((b) => b.kind)).toEqual(['cites']);
  });

  it('produces nothing at all for an empty answer', () => {
    expect(text([])).toEqual([]);
  });
});

describe('sanitization is structural (FR-MSG-07)', () => {
  it('treats a script tag as ordinary characters in ONE text node', () => {
    // Nothing parses `<`. The string survives verbatim to a React child, which React escapes at
    // the DOM boundary — there is no path from this input to an element.
    const blocks = text([{ text: '<script>alert(1)</script>' }]);
    const para = blocks[0] as Extract<Block, { kind: 'para' }>;
    expect(para.children).toEqual([{ kind: 'text', text: '<script>alert(1)</script>' }]);
  });

  it('treats an img tag with an onerror handler as text', () => {
    const blocks = text([{ text: '<img src=x onerror=alert(1)>' }]);
    expect((blocks[0] as Extract<Block, { kind: 'para' }>).children).toEqual([
      { kind: 'text', text: '<img src=x onerror=alert(1)>' },
    ]);
  });

  it('does not decode HTML entities', () => {
    // Text in, text out. Decoding would re-open entity smuggling for no benefit.
    const blocks = text([{ text: '&lt;b&gt; &#x3C;script&#x3E;' }]);
    expect((blocks[0] as Extract<Block, { kind: 'para' }>).children).toEqual([
      { kind: 'text', text: '&lt;b&gt; &#x3C;script&#x3E;' },
    ]);
  });

  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    'data:text/html;base64,PHNjcmlwdD4=',
    'vbscript:msgbox(1)',
    'example.com/relative',
    '/internal/path',
  ])('refuses the scheme in [x](%s) and renders the source literally', (href) => {
    const blocks = text([{ text: `[x](${href})` }]);
    const children = (blocks[0] as Extract<Block, { kind: 'para' }>).children;
    expect(kinds(children)).toEqual(['text']);
    expect((children[0] as Extract<Inline, { kind: 'text' }>).text).toBe(`[x](${href})`);
  });

  it('cannot be tricked by whitespace or control characters inside the scheme', () => {
    // The probe strips everything below U+0021 before testing, so a tab or newline cannot hide
    // `javascript:` from the allow-list.
    const blocks = text([{ text: '[x](java\tscript:alert(1))' }]);
    expect(kinds((blocks[0] as Extract<Block, { kind: 'para' }>).children)).toEqual(['text']);
  });

  it.each(['https://example.com', 'http://example.com', 'mailto:a@b.com'])('accepts %s', (href) => {
    const blocks = text([{ text: `[x](${href})` }]);
    const children = (blocks[0] as Extract<Block, { kind: 'para' }>).children;
    expect(kinds(children)).toEqual(['link']);
    expect((children[0] as Extract<Inline, { kind: 'link' }>).href).toBe(href);
  });
});

describe('inline constructs', () => {
  it('parses bold and italic, single and double markers', () => {
    const blocks = text([{ text: 'a **b** c *d* e __f__ g _h_' }]);
    expect(kinds((blocks[0] as Extract<Block, { kind: 'para' }>).children)).toEqual([
      'text',
      'strong',
      'text',
      'em',
      'text',
      'strong',
      'text',
      'em',
    ]);
  });

  it('makes inline code opaque — no emphasis and no links inside', () => {
    const blocks = text([{ text: 'run `a **b** [c](https://x)` now' }]);
    const children = (blocks[0] as Extract<Block, { kind: 'para' }>).children;
    expect(kinds(children)).toEqual(['text', 'code', 'text']);
    expect((children[1] as Extract<Inline, { kind: 'code' }>).text).toBe('a **b** [c](https://x)');
  });

  it('honours backslash escapes, so a literal asterisk is writable', () => {
    const blocks = text([{ text: 'a \\*not italic\\* b' }]);
    expect((blocks[0] as Extract<Block, { kind: 'para' }>).children).toEqual([
      { kind: 'text', text: 'a *not italic* b' },
    ]);
  });

  it('leaves an unclosed marker as literal text', () => {
    const blocks = text([{ text: 'a * b' }]);
    expect((blocks[0] as Extract<Block, { kind: 'para' }>).children).toEqual([
      { kind: 'text', text: 'a * b' },
    ]);
  });
});

describe('block constructs', () => {
  /** Emitted heading levels, in document order. */
  const levelsOf = (source: string) =>
    text([{ text: source }])
      .filter((b) => b.kind === 'heading')
      .map((b) => (b as Extract<Block, { kind: 'heading' }>).level);

  it('starts a message at h3 and descends one level per nesting step', () => {
    // T-502 owns the only <h1> and T-504's chat title is the <h2>, so a content-supplied
    // heading must start below both rather than competing with them in the outline. R-97 maps
    // by OUTLINE POSITION, not by `#` count: `#` + 2 emitted an <h5> directly after the chat
    // title's <h2> for any answer opening `### ...`, which is the T-721 skip.
    expect(levelsOf('# a\n\n## b\n\n#### d\n\n###### f')).toEqual([3, 4, 5, 6]);
  });

  it('rebases a message whose headings do not start at `#`', () => {
    // The T-721 case verbatim: an answer opening at `###` still starts at <h3>.
    expect(levelsOf('### a\n\n#### b')).toEqual([3, 4]);
  });

  it('keeps sibling headings at one level, however many there are', () => {
    // The `## A / ## B / ## C` shape, which is what a long answer actually looks like — and the
    // one case `heading-order` cannot police, since nesting each section one deeper than the
    // last descends by exactly one every time. Caught by mutation (`>=` -> `>`), not by axe.
    expect(levelsOf('## a\n\n## b\n\n## c\n\n## d\n\n## e')).toEqual([3, 3, 3, 3, 3]);
    expect(levelsOf('# a\n\n## b\n\n## c\n\n# d')).toEqual([3, 4, 4, 3]);
  });

  it('treats a shallower heading as a new top-level section, not a deeper one', () => {
    expect(levelsOf('#### a\n\n## b\n\n### c')).toEqual([3, 3, 4]);
  });

  it('clamps beyond four nesting levels at h6', () => {
    expect(levelsOf('# a\n\n## b\n\n### c\n\n#### d\n\n##### e')).toEqual([3, 4, 5, 6, 6]);
  });

  it('never skips a level, for any `#` sequence', () => {
    // The property axe's `heading-order` checks, asserted directly. A plain shift by the
    // shallowest heading present would pass every case above and fail the first of these.
    const sources = [
      '# a\n\n### b',
      '###### a\n\n# b\n\n###### c',
      '## a\n\n## b\n\n##### c\n\n# d',
      '##### a\n\n#### b\n\n### c\n\n## d\n\n# e',
      '# a\n\n## b\n\n### c\n\n#### d\n\n##### e\n\n###### f',
    ];
    for (const source of sources) {
      const levels = levelsOf(source);
      expect(levels[0]).toBe(3);
      for (let i = 1; i < levels.length; i += 1) expect(levels[i] - levels[i - 1]).toBeLessThan(2);
    }
  });

  it('normalises each message independently', () => {
    // One array per parse call, so the stack cannot leak: two answers both open at <h3>.
    expect(levelsOf('#### a')).toEqual([3]);
    expect(levelsOf('#### b')).toEqual([3]);
  });

  it('does not treat seven hashes as a heading', () => {
    expect(text([{ text: '####### g' }])[0].kind).toBe('para');
  });

  it('parses ordered and unordered lists', () => {
    const ul = text([{ text: '- a\n- b' }])[0] as Extract<Block, { kind: 'list' }>;
    expect(ul.ordered).toBe(false);
    expect(ul.items).toHaveLength(2);
    const ol = text([{ text: '1. a\n2. b' }])[0] as Extract<Block, { kind: 'list' }>;
    expect(ol.ordered).toBe(true);
  });

  it('nests one level on indentation', () => {
    const list = text([{ text: '- a\n  - a1\n- b' }])[0] as Extract<Block, { kind: 'list' }>;
    expect(list.items).toHaveLength(2);
    expect(list.items[0].nested?.items).toHaveLength(1);
  });

  it('keeps a fence verbatim, markdown metacharacters and all', () => {
    const block = text([{ text: '```py\nx = a[*b] | #1\n```' }])[0] as Extract<
      Block,
      { kind: 'code' }
    >;
    expect(block.kind).toBe('code');
    expect(block.info).toBe('py');
    expect(block.text).toBe('x = a[*b] | #1');
  });

  it('parses a pipe table with per-column alignment', () => {
    const table = text([
      { text: '| a | b | c |\n| :-- | :-: | --: |\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |' },
    ])[0] as Extract<Block, { kind: 'table' }>;
    expect(table.kind).toBe('table');
    expect(table.align).toEqual(['left', 'center', 'right']);
    expect(table.head).toHaveLength(3);
    expect(table.rows).toHaveLength(2);
    expect((table.rows[1][2][0] as Extract<Inline, { kind: 'text' }>).text).toBe('6');
  });

  it('is not fooled into a table by a stray pipe', () => {
    expect(text([{ text: 'a | b\nplain text' }])[0].kind).toBe('para');
  });

  it('keeps a wrapped paragraph as one block', () => {
    const blocks = text([{ text: 'line one\nline two\n\nnext' }]);
    expect(blocks.map((b) => b.kind)).toEqual(['para', 'para']);
  });
});

// ─── FR-CIT-07: which block a figure belongs beneath (T-716) ──────────────────

describe('citeIndices', () => {
  it('finds a chip in a paragraph, a heading and a list item', () => {
    const blocks = parse([
      { text: '### Title ' },
      cite(),
      { text: '\n\npara ' },
      cite(),
      { text: '\n\n- item ' },
      cite(),
    ]);

    expect(blocks.map(citeIndices)).toEqual([[0], [1], [2]]);
  });

  it('finds a chip nested inside emphasis and a link', () => {
    // The walk has to recurse: a chip inside `**bold**` is two levels down, and missing it
    // would put its figure beneath the wrong block rather than failing loudly.
    const blocks = parse([{ text: '**bold ' }, cite(), { text: '** tail' }]);

    expect(blocks.map(citeIndices)).toEqual([[0]]);
  });

  it('finds the chips of a table and of a nested list', () => {
    const table = parse([{ text: '| a |\n| - |\n| x ' }, cite(), { text: ' |' }]);
    expect(table.flatMap(citeIndices)).toEqual([0]);

    const nested = parse([{ text: '- outer\n  - inner ' }, cite()]);
    expect(nested.flatMap(citeIndices)).toEqual([0]);
  });

  it('finds the chips of a synthesised `cites` block', () => {
    // `placeOrphans` moves a chip a code block or table cannot hold into its own block; its
    // figure belongs beneath that, which only works if the walk looks there too.
    const blocks = parse([{ text: '```\ncode\n```\n\n' }, cite()]);
    const cites = blocks.filter((b) => b.kind === 'cites');

    expect(cites).toHaveLength(1);
    expect(cites.flatMap(citeIndices)).toEqual([0]);
  });

  it('reports a code block as citing nothing', () => {
    expect(parse([{ text: '```\ncode\n```' }]).map(citeIndices)).toEqual([[]]);
  });
});

describe('renderBlockFigures', () => {
  const classes = {
    paragraph: 'p',
    heading: 'h',
    list: 'l',
    listItem: 'li',
    code: 'c',
    pre: 'pre',
    tableWrap: 'tw',
    table: 't',
    link: 'a',
    alignLeft: 'al',
    alignCenter: 'ac',
    alignRight: 'ar',
  };

  it('is asked once per block, with that block’s own indices', () => {
    const asked: number[][] = [];
    renderMarkdown([{ text: 'one ' }, cite(), { text: '\n\ntwo ' }, cite()], {
      classes,
      renderCitation: () => null,
      renderBlockFigures: (indices) => {
        asked.push([...indices]);
        return null;
      },
    });

    // FR-CIT-07 is "beneath the citing passage", so each block is asked about its own chips —
    // not about every chip in the answer.
    expect(asked).toEqual([[0], [1]]);
  });

  it('inserts what it returns immediately after that block', () => {
    const nodes = renderMarkdown([{ text: 'one ' }, cite(), { text: '\n\ntwo' }], {
      classes,
      renderCitation: () => null,
      renderBlockFigures: (indices) => (indices.length > 0 ? 'FIG' : null),
    });

    // Three nodes: the citing paragraph, its figure, then the paragraph that cites nothing.
    expect(nodes).toHaveLength(3);
    expect(nodes[1]).toMatchObject({ props: { children: 'FIG' } });
  });

  it('changes nothing at all when it is absent or returns null', () => {
    const segs: Segment[] = [{ text: 'one ' }, cite(), { text: '\n\ntwo' }];
    const base = renderMarkdown(segs, { classes, renderCitation: () => null });
    const withHook = renderMarkdown(segs, {
      classes,
      renderCitation: () => null,
      renderBlockFigures: () => null,
    });

    // The FR-CIT-07 promise that an answer with no figures renders exactly as it did before.
    expect(withHook).toHaveLength(base.length);
  });
});
