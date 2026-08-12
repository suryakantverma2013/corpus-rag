/**
 * FR-MSG-07 — Markdown rendering for AI answers, with the FR-CIT-01 chips preserved inline.
 *
 * **Why this is in-tree rather than a dependency, and why that makes it safer rather than
 * riskier.** FR-MSG-07 requires rendering to be "sanitized (no raw HTML/script execution) so
 * document content cannot inject markup". Every off-the-shelf pipeline reaches that by *building*
 * an HTML string and then removing the dangerous parts of it — `marked` + DOMPurify, or
 * `react-markdown` + `rehype-sanitize`, both ending at `dangerouslySetInnerHTML`. This module
 * never forms a string of markup at all: parsing produces a plain-JSON AST, rendering maps that
 * AST through `createElement`, and every leaf is a JavaScript string passed as a React *child*,
 * which React escapes at the DOM boundary. There is nothing for a sanitizer to sanitize, because
 * there is no code path from a `<` in the input to an element. Adding DOMPurify here would mean
 * first constructing the hazard it removes.
 *
 * The one attribute whose value derives from content is a link's `href`, and it is allow-listed
 * to `http:` / `https:` / `mailto:` (see `safeHref`). Class names come only from the injected
 * `classes` map, so content can never reach one.
 *
 * **How citation chips survive.** `flatten` concatenates only the *text* segments into one source
 * string and records each citation as a numeric **offset** into it. Two things follow, and both
 * are the point:
 *
 *   1. A list, table or paragraph that straddles a segment boundary parses as **one** block,
 *      because block parsing never sees the seam. Parsing each `segs` entry independently would
 *      turn one list into two — which is exactly what "preserving the inline citation chips
 *      within the rendered flow" is about.
 *   2. The chip's position is an offset, never a sentinel substring. Nothing is ever inserted
 *      into text that came from a document, so document content cannot forge a chip — the same
 *      discipline the backend applies to its own `[S<n>]` markers.
 *
 * **Deliberately unsupported**, each for a reason rather than for lack of time: raw HTML (never
 * parsed — that is what makes the sanitization structural); HTML entities (decoding them
 * re-opens entity smuggling for no benefit — text in, text out); autolinks and bare-URL
 * linkification (where scheme mistakes happen); images (NFR-CMP-03 ships no image assets, and a
 * content-chosen remote `src` is an outbound request on every render); blockquotes and thematic
 * breaks (not in FR-MSG-07's list, and `---` collides with the table delimiter row and with
 * accidental underlining in extracted text); footnotes, task lists, strikethrough and math.
 * Anything unsupported renders as its own literal source text, which is the honest failure.
 *
 * `renderCitation` is **injected, not imported**, so this module depends on no component, stays a
 * `.ts` file, and is testable at the AST level with no React involved at all.
 */
import { createElement, Fragment } from 'react';
import type { ReactNode } from 'react';
import type { CitationSegment, Segment } from '../api';
import { isCitation } from './messages';

// ─── AST ──────────────────────────────────────────────────────────────────────

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strong'; children: Inline[] }
  | { kind: 'em'; children: Inline[] }
  | { kind: 'link'; href: string; children: Inline[] }
  /** A citation chip. `index` addresses `flatten`'s `citations`, never a segment position. */
  | { kind: 'cite'; index: number };

export type Align = 'left' | 'center' | 'right' | null;

export interface ListItem {
  children: Inline[];
  /** One level of nesting; FR-MSG-07 says "lists", not "arbitrarily nested lists". */
  nested?: Block & { kind: 'list' };
}

export type Block =
  | { kind: 'para'; children: Inline[] }
  | { kind: 'heading'; level: 3 | 4 | 5 | 6; children: Inline[] }
  | { kind: 'list'; ordered: boolean; items: ListItem[] }
  | { kind: 'code'; text: string; info: string }
  | { kind: 'table'; head: Inline[][]; rows: Inline[][][]; align: Align[] }
  /** Chips that could not be placed inside the preceding block — see `placeOrphans`. */
  | { kind: 'cites'; indices: number[] };

interface Anchor {
  offset: number;
  index: number;
}

/** A shared, ordered cursor over one block's anchors. */
interface Cursor {
  anchors: Anchor[];
  next: number;
}

// ─── flatten ──────────────────────────────────────────────────────────────────

export interface Flattened {
  source: string;
  citations: CitationSegment[];
  offsets: number[];
}

/**
 * Concatenate the text runs and record where each citation sat.
 *
 * A citation between two text runs lands at the offset the second run starts at, so the chip
 * renders exactly where the backend's `[S<n>]` marker was — see `split_answer_segments`.
 */
export function flatten(segments: readonly Segment[]): Flattened {
  let source = '';
  const citations: CitationSegment[] = [];
  const offsets: number[] = [];
  for (const segment of segments) {
    if (isCitation(segment)) {
      citations.push(segment);
      offsets.push(source.length);
    } else {
      source += segment.text;
    }
  }
  return { source, citations, offsets };
}

// ─── inline ───────────────────────────────────────────────────────────────────

/** Punctuation a backslash may escape. Without this a literal `*` is unwritable. */
const ESCAPABLE = '\\`*_{}[]()#+-.!|>~';

const SAFE_SCHEMES = ['http:', 'https:', 'mailto:'];

/**
 * The module's only content-derived attribute value.
 *
 * Control characters and whitespace are stripped *before* the scheme test, so `java\tscript:` and
 * `java\nscript:` cannot smuggle a scheme past it. A URL with no scheme is rejected rather than
 * treated as relative: a bare `example.com` would otherwise resolve against our own origin, and
 * a document has no business linking within the application.
 */
function safeHref(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  // Filtered by code point rather than by a character-class regex: the class this needs is a
  // control range, which `no-control-regex` flags — correctly in general, and the exception
  // here reads better as a filter anyway.
  const probe = [...trimmed.toLowerCase()].filter((c) => c.charCodeAt(0) > 0x20).join('');
  return SAFE_SCHEMES.some((scheme) => probe.startsWith(scheme)) ? trimmed : null;
}

function pushText(out: Inline[], buffer: string): string {
  if (buffer !== '') out.push({ kind: 'text', text: buffer });
  return '';
}

/** Emit every pending chip whose offset has been reached. */
function drain(out: Inline[], cursor: Cursor, upTo: number, buffer: string): string {
  let text = buffer;
  while (cursor.next < cursor.anchors.length && cursor.anchors[cursor.next].offset <= upTo) {
    text = pushText(out, text);
    out.push({ kind: 'cite', index: cursor.anchors[cursor.next].index });
    cursor.next += 1;
  }
  return text;
}

/**
 * Parse `source[start, end)` as inline content, emitting chips at their absolute offsets.
 *
 * The scanner checks for a pending anchor before consuming each character, so a chip lands
 * wherever the backend put it — including inside a `**bold**` run, because the recursion carries
 * the same cursor down.
 */
function parseInlineRange(source: string, start: number, end: number, cursor: Cursor): Inline[] {
  const out: Inline[] = [];
  let buffer = '';
  let i = start;

  while (i < end) {
    buffer = drain(out, cursor, i, buffer);
    const char = source[i];

    // Escapes first: `\*` must never open emphasis.
    if (char === '\\' && i + 1 < end && ESCAPABLE.includes(source[i + 1])) {
      buffer += source[i + 1];
      i += 2;
      continue;
    }

    // Inline code is scanned before everything else and is opaque — no emphasis, no links,
    // no chips inside it. A run of N backticks closes on the next run of exactly N.
    if (char === '`') {
      let run = 0;
      while (i + run < end && source[i + run] === '`') run += 1;
      const fence = '`'.repeat(run);
      const from = i + run;
      let close = source.indexOf(fence, from);
      while (close !== -1 && close + run < end && source[close + run] === '`') {
        close = source.indexOf(fence, close + run + 1);
      }
      if (close !== -1 && close + run <= end) {
        buffer = pushText(out, buffer);
        out.push({ kind: 'code', text: source.slice(from, close) });
        // Anchors inside a code span are swallowed by it; move them past without emitting,
        // since a chip cannot render inside <code> without breaking its monospace box.
        while (cursor.next < cursor.anchors.length && cursor.anchors[cursor.next].offset < close) {
          cursor.next += 1;
        }
        i = close + run;
        continue;
      }
      // Unclosed: the backticks are literal.
      buffer += fence;
      i += run;
      continue;
    }

    if (char === '*' || char === '_') {
      const double = source[i + 1] === char;
      const marker = double ? char + char : char;
      const close = findClosing(source, i + marker.length, end, marker);
      if (close !== -1) {
        buffer = pushText(out, buffer);
        const children = parseInlineRange(source, i + marker.length, close, cursor);
        out.push(double ? { kind: 'strong', children } : { kind: 'em', children });
        i = close + marker.length;
        continue;
      }
    }

    if (char === '[') {
      const link = matchLink(source, i, end);
      if (link !== null) {
        buffer = pushText(out, buffer);
        const children = parseInlineRange(source, link.textStart, link.textEnd, cursor);
        out.push({ kind: 'link', href: link.href, children });
        // The href region carries no renderable content; skip any anchor inside it.
        while (
          cursor.next < cursor.anchors.length &&
          cursor.anchors[cursor.next].offset < link.end
        ) {
          cursor.next += 1;
        }
        i = link.end;
        continue;
      }
      // Not a link (malformed, or a rejected scheme) — the `[` is an ordinary character and the
      // rest is rescanned as text, so `[x](javascript:alert(1))` renders as its own source.
    }

    buffer += char;
    i += 1;
  }

  buffer = drain(out, cursor, end, buffer);
  pushText(out, buffer);
  return out;
}

/** The next unescaped `marker` in `[from, end)`, or -1. Empty emphasis (`**`) does not open. */
function findClosing(source: string, from: number, end: number, marker: string): number {
  for (let i = from; i + marker.length <= end; i += 1) {
    if (source[i] === '\\') {
      i += 1;
      continue;
    }
    if (source.startsWith(marker, i) && i > from) {
      // `***` — do not let a double marker be closed by the first of a triple.
      if (marker.length === 1 && source[i + 1] === marker) continue;
      return i;
    }
  }
  return -1;
}

interface LinkMatch {
  textStart: number;
  textEnd: number;
  href: string;
  end: number;
}

/** `[text](href)` with a balanced `)` and an allow-listed scheme, else `null`. */
function matchLink(source: string, at: number, end: number): LinkMatch | null {
  let depth = 0;
  let close = -1;
  for (let i = at; i < end; i += 1) {
    if (source[i] === '\\') {
      i += 1;
      continue;
    }
    if (source[i] === '[') depth += 1;
    else if (source[i] === ']') {
      depth -= 1;
      if (depth === 0) {
        close = i;
        break;
      }
    }
  }
  if (close === -1 || source[close + 1] !== '(') return null;
  const hrefEnd = source.indexOf(')', close + 2);
  if (hrefEnd === -1 || hrefEnd >= end) return null;
  const href = safeHref(source.slice(close + 2, hrefEnd));
  if (href === null) return null;
  return { textStart: at + 1, textEnd: close, href, end: hrefEnd + 1 };
}

// ─── blocks ───────────────────────────────────────────────────────────────────

interface Line {
  text: string;
  start: number;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const FENCE = /^\s*(```|~~~)(.*)$/;

function toLines(source: string): Line[] {
  const lines: Line[] = [];
  let start = 0;
  for (const text of source.split('\n')) {
    lines.push({ text, start });
    start += text.length + 1;
  }
  return lines;
}

function endOf(line: Line): number {
  return line.start + line.text.length;
}

interface RawBlock {
  start: number;
  build: (cursor: Cursor) => Block[];
}

/** Cells of a table row, with each cell's absolute content offset. */
function splitRow(line: Line): { text: string; start: number }[] {
  const cells: { text: string; start: number }[] = [];
  let buffer = '';
  let bufferStart = line.start;
  const flush = (at: number) => {
    const leading = buffer.length - buffer.trimStart().length;
    cells.push({ text: buffer.trim(), start: bufferStart + leading });
    buffer = '';
    bufferStart = at;
  };
  for (let i = 0; i < line.text.length; i += 1) {
    if (line.text[i] === '\\' && i + 1 < line.text.length) {
      buffer += line.text[i] + line.text[i + 1];
      i += 1;
      continue;
    }
    if (line.text[i] === '|') {
      flush(line.start + i + 1);
      continue;
    }
    buffer += line.text[i];
  }
  flush(endOf(line));
  // A leading and/or trailing `|` produces an empty edge cell; drop those, never inner ones.
  if (cells.length > 0 && cells[0].text === '') cells.shift();
  if (cells.length > 0 && cells[cells.length - 1].text === '') cells.pop();
  return cells;
}

const DELIMITER_CELL = /^:?-{1,}:?$/;

function alignmentOf(cells: { text: string }[]): Align[] | null {
  if (cells.length === 0 || !cells.every((c) => DELIMITER_CELL.test(c.text))) return null;
  return cells.map((c) => {
    const left = c.text.startsWith(':');
    const right = c.text.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return null;
  });
}

/**
 * Split the source into blocks. Each carries a builder so inline parsing runs *after* anchors
 * have been assigned to blocks — the assignment needs every block's start offset first.
 */
function parseBlocks(source: string): RawBlock[] {
  const lines = toLines(source);
  const blocks: RawBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (line.text.trim() === '') {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line.text);
    if (fence !== null) {
      const marker = fence[1];
      const info = fence[2].trim();
      const bodyStart = i + 1;
      let j = bodyStart;
      while (j < lines.length && !lines[j].text.trimStart().startsWith(marker)) j += 1;
      const text = lines
        .slice(bodyStart, j)
        .map((l) => l.text)
        .join('\n');
      blocks.push({ start: line.start, build: () => [{ kind: 'code', text, info }] });
      i = j + 1;
      continue;
    }

    const heading = HEADING.exec(line.text);
    if (heading !== null) {
      // `#` → h3: T-502 owns the document's only <h1> and T-504's chat title is the <h2>, so a
      // heading supplied by a document starts below both rather than competing with them.
      const level = Math.min(6, heading[1].length + 2) as 3 | 4 | 5 | 6;
      const start = line.start + line.text.length - heading[2].length;
      blocks.push({
        start: line.start,
        build: (cursor) => [
          {
            kind: 'heading',
            level,
            children: parseInlineRange(source, start, endOf(line), cursor),
          },
        ],
      });
      i += 1;
      continue;
    }

    if (line.text.includes('|') && i + 1 < lines.length) {
      const align = alignmentOf(splitRow(lines[i + 1]));
      if (align !== null) {
        const headLine = line;
        let j = i + 2;
        while (j < lines.length && lines[j].text.includes('|') && lines[j].text.trim() !== '')
          j += 1;
        const bodyLines = lines.slice(i + 2, j);
        blocks.push({
          start: line.start,
          build: (cursor) => [
            {
              kind: 'table',
              align,
              head: splitRow(headLine).map((c) =>
                parseInlineRange(source, c.start, c.start + c.text.length, cursor),
              ),
              rows: bodyLines.map((row) =>
                splitRow(row).map((c) =>
                  parseInlineRange(source, c.start, c.start + c.text.length, cursor),
                ),
              ),
            },
          ],
        });
        i = j;
        continue;
      }
    }

    if (LIST_ITEM.test(line.text)) {
      const entries: { indent: number; ordered: boolean; start: number; end: number }[] = [];
      let j = i;
      while (j < lines.length) {
        const item = LIST_ITEM.exec(lines[j].text);
        if (item === null) break;
        entries.push({
          indent: item[1].length,
          ordered: /\d/.test(item[2]),
          start: lines[j].start + lines[j].text.length - item[3].length,
          end: endOf(lines[j]),
        });
        j += 1;
      }
      const ordered = entries[0].ordered;
      blocks.push({
        start: line.start,
        build: (cursor) => {
          const items: ListItem[] = [];
          for (const entry of entries) {
            const children = parseInlineRange(source, entry.start, entry.end, cursor);
            if (entry.indent >= 2 && items.length > 0) {
              const parent = items[items.length - 1];
              parent.nested ??= { kind: 'list', ordered: entry.ordered, items: [] };
              parent.nested.items.push({ children });
            } else {
              items.push({ children });
            }
          }
          return [{ kind: 'list', ordered, items }];
        },
      });
      i = j;
      continue;
    }

    // Paragraph: consecutive lines until a blank one or another block opener. The slice keeps
    // its newlines, so every offset stays exact; CSS collapses them to spaces on render.
    let j = i;
    while (
      j < lines.length &&
      lines[j].text.trim() !== '' &&
      !(
        j > i &&
        (HEADING.test(lines[j].text) || FENCE.test(lines[j].text) || LIST_ITEM.test(lines[j].text))
      )
    ) {
      j += 1;
    }
    const from = line.start;
    const to = endOf(lines[j - 1]);
    blocks.push({
      start: from,
      build: (cursor) => [{ kind: 'para', children: parseInlineRange(source, from, to, cursor) }],
    });
    i = j;
  }

  return blocks;
}

/** Append chips the block could not place — see `parseMarkdown`. */
function placeOrphans(block: Block, indices: number[]): Block[] {
  if (indices.length === 0) return [block];
  const cites: Inline[] = indices.map((index) => ({ kind: 'cite', index }));
  switch (block.kind) {
    case 'para':
    case 'heading':
      return [{ ...block, children: [...block.children, ...cites] }];
    case 'list': {
      const items = [...block.items];
      const last = items[items.length - 1];
      if (last === undefined) return [block, { kind: 'cites', indices }];
      items[items.length - 1] = { ...last, children: [...last.children, ...cites] };
      return [{ ...block, items }];
    }
    default:
      // A chip inside a fence or a table has nowhere legible to go: `inline-flex` inside <pre>
      // is unstylable and inside <thead> is invalid markup. It follows the block instead.
      return [block, { kind: 'cites', indices }];
  }
}

/**
 * Parse an answer into blocks, with each citation placed at its recorded offset.
 *
 * A citation is assigned to the **last block starting at or before** its offset, so one landing
 * between two paragraphs attaches to the claim it supports rather than to the next one, and a
 * trailing citation — much the commonest shape — attaches to the final block.
 */
export function parseMarkdown(source: string, offsets: readonly number[]): Block[] {
  const raw = parseBlocks(source);
  if (raw.length === 0) {
    return offsets.length === 0 ? [] : [{ kind: 'cites', indices: offsets.map((_, i) => i) }];
  }

  const perBlock: Anchor[][] = raw.map(() => []);
  offsets.forEach((offset, index) => {
    // Strictly BEFORE, not at-or-before. An offset equal to the next block's start is the
    // common two-paragraph case: `split_answer_segments` emits the marker *after* the claim it
    // supports, so the following text run begins exactly there — and `start <= offset` would
    // hand the chip to the paragraph it does not belong to. Falls back to the first block for
    // an answer that opens with a citation.
    let target = 0;
    for (let b = 0; b < raw.length; b += 1) {
      if (raw[b].start < offset) target = b;
    }
    perBlock[target].push({ offset, index });
  });

  const blocks: Block[] = [];
  raw.forEach((rawBlock, b) => {
    const cursor: Cursor = { anchors: perBlock[b], next: 0 };
    const built = rawBlock.build(cursor);
    const orphans = cursor.anchors.slice(cursor.next).map((a) => a.index);
    const last = built[built.length - 1];
    blocks.push(...built.slice(0, -1), ...placeOrphans(last, orphans));
  });
  return blocks;
}

// ─── render ───────────────────────────────────────────────────────────────────

export interface MarkdownClasses {
  paragraph: string;
  heading: string;
  list: string;
  listItem: string;
  code: string;
  pre: string;
  tableWrap: string;
  table: string;
  link: string;
  alignLeft: string;
  alignCenter: string;
  alignRight: string;
}

export interface RenderOptions {
  classes: MarkdownClasses;
  /** Supplied by the caller so this module imports no component. */
  renderCitation: (index: number) => ReactNode;
}

function alignClass(align: Align, classes: MarkdownClasses): string | undefined {
  if (align === 'left') return classes.alignLeft;
  if (align === 'center') return classes.alignCenter;
  if (align === 'right') return classes.alignRight;
  return undefined;
}

function renderInline(nodes: readonly Inline[], options: RenderOptions): ReactNode[] {
  return nodes.map((node, i) => {
    const key = `i${i}`;
    switch (node.kind) {
      case 'text':
        return createElement(Fragment, { key }, node.text);
      case 'code':
        return createElement('code', { key, className: `${options.classes.code} mono` }, node.text);
      case 'strong':
        return createElement('strong', { key }, renderInline(node.children, options));
      case 'em':
        return createElement('em', { key }, renderInline(node.children, options));
      case 'link':
        return createElement(
          'a',
          {
            key,
            className: options.classes.link,
            href: node.href,
            target: '_blank',
            rel: 'noopener noreferrer',
          },
          renderInline(node.children, options),
        );
      case 'cite':
        return createElement(Fragment, { key }, options.renderCitation(node.index));
    }
  });
}

function renderList(
  block: Block & { kind: 'list' },
  options: RenderOptions,
  key: string,
): ReactNode {
  return createElement(
    block.ordered ? 'ol' : 'ul',
    { key, className: options.classes.list },
    block.items.map((item, i) =>
      createElement(
        'li',
        { key: `li${i}`, className: options.classes.listItem },
        renderInline(item.children, options),
        item.nested === undefined ? null : renderList(item.nested, options, `n${i}`),
      ),
    ),
  );
}

function renderBlock(block: Block, options: RenderOptions, key: string): ReactNode {
  const { classes } = options;
  switch (block.kind) {
    case 'para':
      return createElement(
        'p',
        { key, className: classes.paragraph },
        renderInline(block.children, options),
      );
    case 'heading':
      return createElement(
        `h${block.level}`,
        { key, className: classes.heading },
        renderInline(block.children, options),
      );
    case 'list':
      return renderList(block, options, key);
    case 'code':
      // The info string is parsed but not rendered: highlighting would mean a dependency and a
      // language chip the prototype does not have. `overflow-x` lives on .pre (FR-MSG-07).
      return createElement(
        'pre',
        { key, className: `${classes.pre} mono` },
        createElement('code', null, block.text),
      );
    case 'table':
      return createElement(
        'div',
        { key, className: classes.tableWrap },
        createElement(
          'table',
          { className: classes.table },
          createElement(
            'thead',
            null,
            createElement(
              'tr',
              null,
              block.head.map((cell, c) =>
                createElement(
                  'th',
                  { key: `h${c}`, className: alignClass(block.align[c] ?? null, classes) },
                  renderInline(cell, options),
                ),
              ),
            ),
          ),
          createElement(
            'tbody',
            null,
            block.rows.map((row, r) =>
              createElement(
                'tr',
                { key: `r${r}` },
                row.map((cell, c) =>
                  createElement(
                    'td',
                    { key: `c${c}`, className: alignClass(block.align[c] ?? null, classes) },
                    renderInline(cell, options),
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    case 'cites':
      return createElement(
        'p',
        { key, className: classes.paragraph },
        block.indices.map((index) =>
          createElement(Fragment, { key: `c${index}` }, options.renderCitation(index)),
        ),
      );
  }
}

/** The whole pipeline: segments in, React elements out, no markup string in between. */
export function renderMarkdown(segments: readonly Segment[], options: RenderOptions): ReactNode[] {
  const { source, offsets } = flatten(segments);
  return parseMarkdown(source, offsets).map((block, i) => renderBlock(block, options, `b${i}`));
}
