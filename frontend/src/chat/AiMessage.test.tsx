/**
 * FR-MSG-04's four-part order, FR-MSG-07's rendered output, FR-EVL-02's chip row and FR-MSG-08's
 * action bar.
 *
 * Class names are never asserted: Vitest stubs CSS Modules with a Proxy that answers *any* key
 * with a truthy hashed string, so `styles.typo` would pass. Structure, roles, text and
 * `data-band` are the seams.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { AiMessage } from './AiMessage';
import { CitationHoverProvider } from './CitationHoverProvider';
import type { CitationSegment, Evaluation, Feedback, Message, Segment } from '../api';

function cite(doc: string, extra: Partial<CitationSegment> = {}): CitationSegment {
  return { isCite: true, doc, quote: 'the quoted passage', chunkId: `${doc}:1`, ...extra };
}

function answer(segs: Segment[], extra: Partial<Message> = {}): Message {
  return { id: 'a1', role: 'ai', segs, created_at: 'x', ...extra };
}

function show(
  message: Message,
  handlers: Partial<{
    onFeedback: (f: Feedback | null) => void;
    onRegenerate: () => void;
    busy: boolean;
  }> = {},
) {
  return render(
    <CitationHoverProvider>
      <AiMessage
        message={message}
        busy={handlers.busy ?? false}
        onFeedback={handlers.onFeedback ?? (() => {})}
        onRegenerate={handlers.onRegenerate ?? (() => {})}
      />
    </CitationHoverProvider>,
  );
}

/** True when `a` precedes `b` in document order. */
function precedes(a: Node, b: Node): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

const BOTH: Evaluation = { relevancy: 0.94, faithfulness: 0.97 };

describe('FR-MSG-04 — the content column’s four parts, in order', () => {
  it('renders body, source line, eval chips, then the action bar', () => {
    show(
      answer([{ text: 'Growth was strong ' }, cite('Q3.pdf'), { text: '.' }], { evaluation: BOTH }),
    );
    const body = screen.getByText(/Growth was strong/);
    const source = screen.getByText(/^grounded in/);
    const chip = screen.getAllByTitle('DeepEval metric')[0];
    const regenerate = screen.getByRole('button', { name: 'Regenerate' });

    expect(precedes(body, source)).toBe(true);
    expect(precedes(source, chip)).toBe(true);
    expect(precedes(chip, regenerate)).toBe(true);
  });

  it('omits the source line when the answer cites nothing', () => {
    show(answer([{ text: 'A plain answer.' }]));
    expect(screen.queryByText(/^grounded in/)).toBeNull();
  });

  it('omits the eval row until the job lands, and for ever if it never does', () => {
    show(answer([{ text: 'A plain answer.' }], { evaluation: null }));
    expect(screen.queryByTitle('DeepEval metric')).toBeNull();
  });
});

describe('FR-EVL-02/03 — the chip row', () => {
  it('renders one chip per metric present, never padded', () => {
    show(answer([{ text: 'x' }], { evaluation: { relevancy: 0.86, faithfulness: null } }));
    const chips = screen.getAllByTitle('DeepEval metric');
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe('Relevancy 0.86');
  });

  it('bands each dot by score, and keeps the numeral beside it (NFR-A11Y-06)', () => {
    const { container } = show(
      answer([{ text: 'x' }], { evaluation: { relevancy: 0.94, faithfulness: 0.75 } }),
    );
    const bands = [...container.querySelectorAll('[data-band]')].map((n) =>
      n.getAttribute('data-band'),
    );
    expect(bands).toEqual(['good', 'bad']);
    // The number is what NFR-A11Y-06's exception depends on: all three FR-EVL-03 hues fail
    // contrast as text in the light theme, so colour may not be the sole carrier.
    expect(screen.getAllByTitle('DeepEval metric')[1].textContent).toBe('Faithfulness 0.75');
  });
});

describe('FR-MSG-04 — the source line', () => {
  it('counts passages and lists distinct documents', () => {
    show(answer([{ text: 'a ' }, cite('Q3.pdf'), { text: ' b ' }, cite('Q3.pdf')]));
    expect(screen.getByText(/^grounded in/).textContent).toBe('grounded in 2 passages · Q3.pdf');
  });

  it('uses the singular at exactly one', () => {
    show(answer([{ text: 'a ' }, cite('Q3.pdf')]));
    expect(screen.getByText(/^grounded in/).textContent).toBe('grounded in 1 passage · Q3.pdf');
  });
});

describe('FR-MSG-07 — rendered Markdown', () => {
  it('renders emphasis, code, lists, headings, links and tables as real elements', () => {
    const { container } = show(
      answer([
        {
          text:
            // `#` maps to <h3>, not <h1>: T-502 owns the document's only <h1> and T-504's chat
            // title is the <h2> inside <main>, so a document-supplied heading starts below both.
            '# Heading\n\nA **bold** word and `code`.\n\n' +
            '- one\n- two\n\n' +
            '| a | b |\n| :-- | --: |\n| 1 | 2 |\n\n' +
            '[link](https://example.com)',
        },
      ]),
    );
    expect(container.querySelector('h3')?.textContent).toBe('Heading');
    expect(container.querySelector('strong')?.textContent).toBe('bold');
    expect(container.querySelector('code')?.textContent).toBe('code');
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelectorAll('table th')).toHaveLength(2);
    const link = container.querySelector('a');
    expect(link?.getAttribute('href')).toBe('https://example.com');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('renders a fenced block as <pre><code>, verbatim', () => {
    const { container } = show(answer([{ text: '```js\nconst a = **1**;\n```' }]));
    const pre = container.querySelector('pre');
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toBe('const a = **1**;');
    expect(pre?.querySelector('strong')).toBeNull();
  });

  it('never turns markup in the answer into elements (sanitized by construction)', () => {
    // The requirement's "document content cannot inject markup", asserted where it is visible:
    // no HTML string is ever built, so the tag survives as text.
    const { container } = show(answer([{ text: '<img src=x onerror=alert(1)> and <b>bold</b>' }]));
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('refuses a javascript: link, rendering its source instead', () => {
    const { container } = show(answer([{ text: '[click](javascript:alert(1))' }]));
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toContain('[click](javascript:alert(1))');
  });

  it('keeps a citation chip inline in the rendered flow', () => {
    // The whole point of the offset-anchored design: the chip is a sibling of the surrounding
    // text inside the SAME paragraph, not a block appended after it.
    const { container } = show(
      answer([{ text: 'growth was strong ' }, cite('Q3.pdf'), { text: ' this quarter.' }]),
    );
    const paragraph = container.querySelector('p');
    expect(paragraph).not.toBeNull();
    const chip = within(paragraph as HTMLElement).getByRole('button', { name: 'Q3.pdf' });
    expect(chip.tagName).toBe('BUTTON');
    expect(paragraph?.textContent).toBe('growth was strong Q3.pdf this quarter.');
  });
});

describe('FR-MSG-08 — the action bar', () => {
  it('sets the rating, and clears it when the active thumb is pressed again', () => {
    const onFeedback = vi.fn();
    const { rerender } = show(answer([{ text: 'x' }]), { onFeedback });
    fireEvent.click(screen.getByRole('button', { name: 'Good answer' }));
    expect(onFeedback).toHaveBeenCalledWith('up');

    rerender(
      <CitationHoverProvider>
        <AiMessage
          message={answer([{ text: 'x' }], { feedback: 'up' })}
          busy={false}
          onFeedback={onFeedback}
          onRegenerate={() => {}}
        />
      </CitationHoverProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Good answer' }));
    // `null`, not `undefined`: FR-MSG-06's third state, and the key is required on the wire.
    expect(onFeedback).toHaveBeenLastCalledWith(null);
  });

  it('switches straight from 👍 to 👎 without an intermediate clear', () => {
    const onFeedback = vi.fn();
    show(answer([{ text: 'x' }], { feedback: 'up' }), { onFeedback });
    fireEvent.click(screen.getByRole('button', { name: 'Poor answer' }));
    expect(onFeedback).toHaveBeenCalledWith('down');
  });

  it('carries the active state in aria-pressed, not only in colour (NFR-A11Y-06)', () => {
    show(answer([{ text: 'x' }], { feedback: 'down' }));
    expect(screen.getByRole('button', { name: 'Good answer' }).getAttribute('aria-pressed')).toBe(
      'false',
    );
    expect(screen.getByRole('button', { name: 'Poor answer' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
  });

  it('disables all three while a turn is generating', () => {
    show(answer([{ text: 'x' }]), { busy: true });
    for (const name of ['Good answer', 'Poor answer', 'Regenerate']) {
      expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it('gives the thumbs a real accessible name rather than a bare emoji', () => {
    show(answer([{ text: 'x' }]));
    expect(screen.queryByRole('button', { name: 'Good answer' })).not.toBeNull();
    expect(screen.queryByRole('button', { name: '👍' })).toBeNull();
  });
});

describe('FR-MSG-08 — Regenerate asks first (R-56(5))', () => {
  it('does not regenerate on the first click', () => {
    const onRegenerate = vi.fn();
    show(answer([{ text: 'x' }]), { onRegenerate });
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    expect(onRegenerate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeNull();
  });

  it('warns that the replacement cannot be undone', () => {
    // R-56(5): a re-run replaces unconditionally — an abstained one replaces a good answer too
    // — and the server keeps no prior version, so the confirmation is the only mitigation.
    show(answer([{ text: 'x' }]));
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    expect(within(screen.getByRole('dialog')).getByText(/cannot be undone/)).not.toBeNull();
  });

  it('regenerates once the user confirms', () => {
    const onRegenerate = vi.fn();
    show(answer([{ text: 'x' }]), { onRegenerate });
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Regenerate' }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('cancels without regenerating, and returns focus to the trigger', () => {
    const onRegenerate = vi.fn();
    show(answer([{ text: 'x' }]), { onRegenerate });
    const trigger = screen.getByRole('button', { name: 'Regenerate' });
    // `fireEvent.click` does not move focus in jsdom, where a real pointer click does — so the
    // focus the dialog is expected to restore has to be established explicitly. Without this the
    // test asserts jsdom's shortcoming rather than the NFR-A11Y-04 behaviour.
    trigger.focus();
    fireEvent.click(trigger);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(onRegenerate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
    // NFR-A11Y-04: dismissing must not drop focus to <body>.
    expect(document.activeElement).toBe(trigger);
  });

  it('cancels on Escape', () => {
    const onRegenerate = vi.fn();
    show(answer([{ text: 'x' }]), { onRegenerate });
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onRegenerate).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
