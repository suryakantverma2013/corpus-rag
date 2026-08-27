/**
 * §4.4 behaviour: FR-CMP-01..06, NFR-USE-03, NFR-A11Y-03/04 and FR-STA-04's block.
 *
 * The FR-CMP-05 matrix gets one test per row, because the spec enumerates it and because two
 * of those rows are counter-intuitive enough that a future simplification would look like a
 * cleanup.
 *
 * Two conventions inherited from the eleven test files before this one, both deliberate:
 * `fireEvent` rather than `@testing-library/user-event` (the matrix is defined by what
 * `onChange` assigns, which `fireEvent.change` reproduces exactly, and a second interaction
 * library for one task is not worth the dependency), and **plain vitest matchers** — jest-dom
 * is not installed, so attributes are read with `getAttribute` rather than `toHaveAttribute`.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Composer } from './Composer';
import type { MentionDocument } from './mentions';

const DOCUMENTS: MentionDocument[] = [
  { id: '1', name: 'Project_Alpha_Brief.pdf', type: 'PDF', meta: '24 pages', scope: 'chat' },
  { id: '2', name: 'Q3_Market_Report.pdf', type: 'PDF', meta: '58 pages', scope: 'global' },
  { id: '3', name: 'Customer_Feedback.csv', type: 'CSV', meta: '1,204 rows', scope: 'global' },
];

/** Roomy enough that nothing here trips FR-STA-04 by accident. */
const ROOMY = { used: 0, limit: 10_400, reserve: 1_500 };

function setup(props: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSend = vi.fn();
  const onOpenKnowledgeBase = vi.fn();
  render(
    <Composer
      documents={DOCUMENTS}
      pending={false}
      usage={ROOMY}
      documentCount={DOCUMENTS.length}
      onSend={onSend}
      onOpenKnowledgeBase={onOpenKnowledgeBase}
      {...props}
    />,
  );
  return { onSend, onOpenKnowledgeBase };
}

const input = () => screen.getByRole('textbox') as HTMLTextAreaElement;
const menu = () => screen.queryByRole('listbox');
const atButton = () => screen.getByRole('button', { name: 'Reference a document' });
const addButton = () => screen.getByRole('button', { name: 'Add documents' });
const sendButton = () => screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
const options = () => screen.getAllByRole('option');

/** One keystroke's worth of controlled-input update. */
const type = (value: string) => fireEvent.change(input(), { target: { value } });
const press = (key: string, modifiers: Record<string, boolean> = {}) =>
  fireEvent.keyDown(input(), { key, ...modifiers });
/** Rows select on mousedown, not click — see `MentionMenu`. */
const chooseRow = (name: RegExp) => fireEvent.mouseDown(screen.getByRole('option', { name }));

describe('FR-CMP-01 — the input bar', () => {
  it('renders the placeholder verbatim from §9', () => {
    setup();
    expect(input().getAttribute('placeholder')).toBe('Ask a question — @ to cite a document');
  });

  it('gives the input an accessible name although the design shows no label', () => {
    setup();
    // NFR-A11Y-03, asserted through the label association rather than the placeholder — a
    // placeholder vanishes on the first keystroke and is not an accessible name.
    expect(screen.getByLabelText('Ask a question')).toBe(input());
  });

  it('has @, + and Send as real buttons (NFR-A11Y-03)', () => {
    setup();
    for (const button of [atButton(), addButton(), sendButton()]) {
      expect(button.tagName).toBe('BUTTON');
    }
  });
});

describe('FR-CMP-02 — the two icon buttons', () => {
  it("keeps the prototype's native tooltips", () => {
    setup();
    expect(atButton().getAttribute('title')).toBe('Reference a document');
    expect(addButton().getAttribute('title')).toBe('Add documents');
  });

  it('opens the KB modal from + and closes the menu with it', () => {
    const { onOpenKnowledgeBase } = setup();
    type('@');
    expect(menu()).not.toBeNull();

    fireEvent.click(addButton());

    expect(onOpenKnowledgeBase).toHaveBeenCalledTimes(1);
    expect(menu()).toBeNull();
  });
});

describe('FR-CMP-05 — the open/close matrix', () => {
  it('opens when the value comes to end with @', () => {
    setup();
    expect(menu()).toBeNull();
    type('what does @');
    expect(menu()).not.toBeNull();
  });

  it('closes as soon as the value ceases to end with @', () => {
    setup();
    type('@');
    expect(menu()).not.toBeNull();
    type('@x');
    expect(menu()).toBeNull();
  });

  it('opens from the @ button with no trailing @ in the value', () => {
    setup();
    fireEvent.click(atButton());
    expect(menu()).not.toBeNull();
    expect(input().value).toBe('');
  });

  it('toggles closed from the @ button', () => {
    setup();
    fireEvent.click(atButton());
    fireEvent.click(atButton());
    expect(menu()).toBeNull();
  });

  it('closes a BUTTON-opened menu on the next keystroke (Rev 0.5.1 legacy delta)', () => {
    // The counter-intuitive row, and the one a "smarter" implementation breaks: the trailing-@
    // condition is re-evaluated on every keystroke *regardless of how the menu was opened*.
    setup();
    fireEvent.click(atButton());
    expect(menu()).not.toBeNull();

    type('a');

    expect(menu()).toBeNull();
  });

  it('closes on Escape (NFR-USE-03)', () => {
    setup();
    type('@');
    press('Escape');
    expect(menu()).toBeNull();
  });

  it('does not send on the Escape that closes the menu', () => {
    const { onSend } = setup();
    type('hello @');
    press('Escape');
    expect(onSend).not.toHaveBeenCalled();
  });

  it('inserts @Filename and closes on row selection', () => {
    setup();
    type('what does @');
    chooseRow(/Q3_Market_Report\.pdf/);

    expect(input().value).toBe('what does @Q3_Market_Report.pdf ');
    expect(menu()).toBeNull();
  });

  it('closes on send', () => {
    setup();
    type('hello');
    fireEvent.click(atButton());
    expect(menu()).not.toBeNull();

    fireEvent.click(sendButton());

    expect(menu()).toBeNull();
  });
});

describe('FR-CMP-04 — the menu contents', () => {
  it('is headed REFERENCE A DOCUMENT and lists every document in scope', () => {
    setup();
    type('@');
    expect(screen.getByText('REFERENCE A DOCUMENT')).not.toBeNull();
    expect(options()).toHaveLength(DOCUMENTS.length);
  });

  it('renders the badge, the filename and the meta', () => {
    setup();
    type('@');
    const row = screen.getByRole('option', { name: /Customer_Feedback\.csv/ });
    expect(within(row).getByText('CSV')).not.toBeNull();
    expect(within(row).getByText('1,204 rows')).not.toBeNull();
  });

  it('shows "this chat" instead of size metadata for an attachment', () => {
    setup();
    type('@');
    const row = screen.getByRole('option', { name: /Project_Alpha_Brief\.pdf/ });
    expect(within(row).getByText('this chat')).not.toBeNull();
    expect(within(row).queryByText('24 pages')).toBeNull();
  });

  it('says so rather than showing an empty popover with no documents', () => {
    setup({ documents: [] });
    type('@');
    expect(screen.getByText('No documents yet')).not.toBeNull();
  });
});

describe('FR-CMP-03 — send', () => {
  it('sends the trimmed text and clears the input', () => {
    const { onSend } = setup();
    type('  hello  ');
    fireEvent.click(sendButton());

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('hello');
    expect(input().value).toBe('');
  });

  it('sends on Enter (NFR-USE-03)', () => {
    const { onSend } = setup();
    type('hello');
    press('Enter');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('hello');
  });

  it('does not send on Shift+Enter, so the browser inserts a newline (R-91(2))', () => {
    const { onSend } = setup();
    type('first line');
    press('Enter', { shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('sends on Ctrl+Enter and on Cmd+Enter (R-91(2))', () => {
    const { onSend } = setup();
    type('hello');
    press('Enter', { ctrlKey: true });
    expect(onSend).toHaveBeenCalledWith('hello');

    type('again');
    press('Enter', { metaKey: true });
    expect(onSend).toHaveBeenCalledWith('again');
  });

  it('carries a multi-line draft through to onSend with its newlines intact', () => {
    // The whole point of R-91: the transcript has rendered `white-space: pre-wrap` since T-505,
    // so the line structure has to survive the composer to be worth anything.
    const { onSend } = setup();
    type('one\ntwo\nthree');
    press('Enter');
    expect(onSend).toHaveBeenCalledWith('one\ntwo\nthree');
  });

  it('still ignores a draft that is only newlines and spaces', () => {
    const { onSend } = setup();
    type('\n  \n');
    press('Enter');
    expect(onSend).not.toHaveBeenCalled();
    expect(sendButton().disabled).toBe(true);
  });

  it('ignores an empty or whitespace-only send, by both triggers', () => {
    const { onSend } = setup();
    type('   ');
    press('Enter');
    expect(onSend).not.toHaveBeenCalled();
    expect(sendButton().disabled).toBe(true);
  });

  it('ignores a send while a reply is pending, including via Enter', () => {
    // A disabled button does not disable a key — the guard must be in the handler too.
    const { onSend } = setup({ pending: true });
    type('hello');
    press('Enter');
    expect(onSend).not.toHaveBeenCalled();
    expect(sendButton().disabled).toBe(true);
  });
});

describe('FR-STA-04 — the pre-submission block', () => {
  // 8_900 used + 1_500 reserve = 10_400, so the chat is exactly full: any query overflows.
  const FULL = { used: 8_900, limit: 10_400, reserve: 1_500 };

  it('blocks a send that would exceed the budget once answered', () => {
    const { onSend } = setup({ usage: FULL });
    type('one more question');
    press('Enter');
    expect(onSend).not.toHaveBeenCalled();
    expect(sendButton().disabled).toBe(true);
  });

  it('allows a send that fits exactly, ties being permissive', () => {
    // 8_896 + ceil(4/4) + 1_500 = 10_400 — on the limit, which `check_submission` allows.
    const { onSend } = setup({ usage: { ...FULL, used: 8_896 } });
    type('abcd');
    press('Enter');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('abcd');
  });

  it('re-enables as the draft is shortened, since that is what FR-STA-04 asks for', () => {
    setup({ usage: { ...FULL, used: 8_880 } });

    type('x'.repeat(100));
    expect(sendButton().disabled).toBe(true);

    type('short');
    expect(sendButton().disabled).toBe(false);
  });
});

describe('FR-CMP-06 — the footer line', () => {
  it('renders the count and the Enter hint', () => {
    setup({ documentCount: 5 });
    expect(
      screen.getByText(
        'Responses grounded in 5 documents · Enter to send, Shift+Enter for a new line',
      ),
    ).not.toBeNull();
  });
});

describe('NFR-A11Y-03/04 — the listbox contract', () => {
  it('is a plain textbox: ARIA has no combobox role for a <textarea> (R-96)', () => {
    setup();
    // Both absences together, because dropping only the role is not a fix -- `aria-expanded`
    // is not permitted on a textbox either, and axe reports that instead. Asserted as the
    // *absence* rather than by querying the role, because `getByRole('textbox')` above would
    // keep passing if someone re-added `role="combobox"` to a control that also has a label.
    expect(input().getAttribute('role')).toBeNull();
    expect(input().getAttribute('aria-expanded')).toBeNull();
  });

  it('identifies the listbox it controls', () => {
    setup();
    expect(input().getAttribute('aria-controls')).toBeNull();

    type('@');

    expect(input().getAttribute('aria-controls')).toBe(menu()?.id);
  });

  it('announces the open menu, since the field can no longer report expanded (R-96)', () => {
    setup();
    const region = () => document.querySelector('[aria-live="polite"]');
    // Present and empty while shut: a live region that only enters the DOM once it has text is
    // frequently never announced, because the AT did not observe it change.
    expect(region()).not.toBeNull();
    expect(region()?.textContent).toBe('');

    type('@');

    // Count taken from the fixture, so adding a row to DOCUMENTS cannot silently make this
    // assert the wrong number while still passing on the wording.
    expect(region()?.textContent).toBe(
      `${DOCUMENTS.length} documents available. Use the arrow keys to reference one.`,
    );
  });

  it('says "1 document" rather than "1 documents"', () => {
    setup({ documents: [DOCUMENTS[0]] });
    type('@');
    expect(document.querySelector('[aria-live="polite"]')?.textContent).toBe(
      '1 document available. Use the arrow keys to reference one.',
    );
  });

  it('keeps the @ button as the second carrier of the expanded state', () => {
    setup();
    expect(atButton().getAttribute('aria-expanded')).toBe('false');
    type('@');
    expect(atButton().getAttribute('aria-expanded')).toBe('true');
  });

  it('opens with no active option, so Enter still sends', () => {
    const { onSend } = setup();
    type('hello @');
    expect(input().getAttribute('aria-activedescendant')).toBeNull();

    press('Enter');

    // The trailing `@` is part of the message; what matters is that Enter sent rather than
    // selecting a row the user never moved to.
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('hello @');
  });

  it('moves a virtual focus with the arrow keys and keeps DOM focus in the input', () => {
    setup();
    // Focused explicitly because `fireEvent.change` does not focus the way real typing does;
    // the claim under test is that ArrowDown moves the *virtual* focus and leaves DOM focus
    // where it was, which is only meaningful if it was in the input to begin with.
    input().focus();
    type('@');
    press('ArrowDown');

    expect(document.activeElement).toBe(input());
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[0].id);
    expect(options()[0].getAttribute('aria-selected')).toBe('true');

    press('ArrowDown');
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[1].id);
    expect(options()[0].getAttribute('aria-selected')).toBe('false');
  });

  it('wraps upward from the top', () => {
    setup();
    type('@');
    press('ArrowUp');
    expect(input().getAttribute('aria-activedescendant')).toBe(options().at(-1)?.id);
  });

  it('selects the active option with Enter instead of sending', () => {
    const { onSend } = setup();
    type('what does @');
    press('ArrowDown');
    press('Enter');

    expect(onSend).not.toHaveBeenCalled();
    expect(input().value).toBe('what does @Project_Alpha_Brief.pdf ');
  });

  it('keeps the rows out of the tab order, so five documents are not a keyboard trap', () => {
    setup();
    type('@');
    for (const option of options()) {
      expect(option.getAttribute('tabindex')).toBe('-1');
    }
  });
});
