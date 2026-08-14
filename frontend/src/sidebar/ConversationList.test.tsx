import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ConversationList } from './ConversationList';
import type { SidebarConversation } from './conversations';

const conversation = (id: string, title: string | null, count = 2): SidebarConversation => ({
  id,
  title,
  archived: false,
  created_at: '2026-07-16T09:12:00Z',
  updated_at: '2026-07-16T09:12:00Z',
  message_count: count,
});

const CONVERSATIONS = [
  conversation('a', 'Analyzing Market Trends'),
  conversation('b', 'Product Launch Strategy'),
  conversation('c', null, 0),
];

function list(overrides: Partial<React.ComponentProps<typeof ConversationList>> = {}) {
  const props = {
    conversations: CONVERSATIONS,
    activeId: 'a',
    onSelect: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(async () => null),
    ...overrides,
  };
  return { ...render(<ConversationList {...props} />), props };
}

/** Opens the ⋯ menu for a row and returns its menu element. */
function openMenu(name: string) {
  fireEvent.click(screen.getByRole('button', { name: `Actions for ${name}` }));
  return screen.getByRole('menu');
}

describe('FR-SBR-03 the list', () => {
  it('is a real list, labelled by the CONVERSATIONS heading', () => {
    // NFR-A11Y-03: a <ul> of <button>s, not the prototype's clickable <div>s.
    list();
    const heading = screen.getByRole('heading', { level: 2, name: 'CONVERSATIONS' });
    expect(screen.getByRole('list').getAttribute('aria-labelledby')).toBe(heading.id);
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
  });

  it('renders each row’s title, date and message count', () => {
    list();
    const row = screen.getByRole('button', { name: /^Analyzing Market Trends/ });
    expect(row.textContent).toContain('Analyzing Market Trends');
    expect(row.textContent).toContain('Jul 16');
    expect(row.textContent).toContain('· 2 messages');
  });

  it('titles an untitled conversation "New chat"', () => {
    list();
    expect(screen.getByRole('button', { name: /^New chat/ })).not.toBeNull();
  });

  it('marks only the active row as current', () => {
    list();
    const current = screen
      .getAllByRole('button')
      .filter((b) => b.getAttribute('aria-current') === 'true');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain('Analyzing Market Trends');
  });

  it('marks nothing current when there is no active conversation', () => {
    list({ activeId: null });
    expect(screen.queryByRole('button', { current: true })).toBeNull();
  });
});

describe('FR-SBR-04 selection', () => {
  it('reports the clicked conversation', () => {
    const { props } = list();
    fireEvent.click(screen.getByRole('button', { name: /^Product Launch Strategy/ }));
    expect(props.onSelect).toHaveBeenCalledWith('b');
  });
});

describe('FR-SBR-07 the overflow control', () => {
  it('is always in the DOM, so it is reachable by keyboard (NFR-A11Y-04)', () => {
    // The load-bearing assertion for this requirement. Revealed by opacity alone — `display:
    // none` or `visibility: hidden` would look identical to a mouse user and make the
    // affordance pointer-only, which is the exact defect NFR-A11Y-04 names.
    list();
    const triggers = screen.getAllByRole('button', { name: /^Actions for / });
    expect(triggers).toHaveLength(3);
    for (const trigger of triggers) {
      expect(trigger.hasAttribute('hidden')).toBe(false);
      expect(trigger.getAttribute('tabindex')).toBeNull();
    }
  });

  it('names the row it acts on', () => {
    // Twenty buttons all called "More" are indistinguishable in a screen reader's control list.
    list();
    expect(
      screen.getByRole('button', { name: 'Actions for Analyzing Market Trends' }),
    ).not.toBeNull();
  });

  it('opens a menu with Rename and Delete, and reports expansion', () => {
    list();
    const trigger = screen.getByRole('button', { name: 'Actions for Analyzing Market Trends' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');

    const menu = openMenu('Analyzing Market Trends');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(
      within(menu)
        .getAllByRole('menuitem')
        .map((i) => i.textContent),
    ).toEqual(['Rename', 'Delete']);
  });

  it('focuses the first item on open', () => {
    list();
    const menu = openMenu('Analyzing Market Trends');
    expect(document.activeElement).toBe(within(menu).getByRole('menuitem', { name: 'Rename' }));
  });

  it('cycles items with the arrow keys', () => {
    list();
    const menu = openMenu('Analyzing Market Trends');
    const [rename, del] = within(menu).getAllByRole('menuitem');

    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(del);
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(rename);
    fireEvent.keyDown(menu, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(del);
  });

  it('closes on Tab rather than letting focus walk out of an open menu (NFR-A11Y-04)', () => {
    // T-511, measured live: the items are ordinary tab stops and this handler only fires while
    // focus is INSIDE the menu, so tabbing off the last item left the menu open with focus on
    // an unrelated conversation row — and Escape then went to that row instead, so the popover
    // could not be dismissed from the keyboard at all. Tab now dismisses it, as the ARIA menu
    // pattern expects, and focus returns to the trigger exactly as Escape leaves it.
    list();
    const menu = openMenu('Analyzing Market Trends');
    fireEvent.keyDown(menu, { key: 'Tab' });

    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: 'Actions for Analyzing Market Trends' }),
    );
  });

  it('closes on Escape and returns focus to the trigger', () => {
    list();
    const menu = openMenu('Analyzing Market Trends');
    fireEvent.keyDown(menu, { key: 'Escape' });

    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: 'Actions for Analyzing Market Trends' }),
    );
  });

  it('closes when the list scrolls', () => {
    // The menu is `position: fixed` at coordinates captured when it opened, so a scroll would
    // otherwise leave it floating beside the wrong row.
    list();
    openMenu('Analyzing Market Trends');
    fireEvent.scroll(screen.getByRole('list'));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes on a pointer press outside it', () => {
    list();
    openMenu('Analyzing Market Trends');
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('opens only one menu at a time', () => {
    list();
    openMenu('Analyzing Market Trends');
    openMenu('Product Launch Strategy');
    expect(screen.getAllByRole('menu')).toHaveLength(1);
  });
});

describe('FR-SBR-07 rename', () => {
  const startRename = (name: string) => {
    const menu = openMenu(name);
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Rename' }));
    return screen.getByRole('textbox');
  };

  it('edits the title in place, seeded with the current one', () => {
    list();
    expect(startRename('Analyzing Market Trends')).toHaveProperty(
      'value',
      'Analyzing Market Trends',
    );
    // The row's button is replaced while editing — an <input> inside a <button> is invalid
    // markup and unusable, so the two states are exclusive.
    expect(screen.queryByRole('button', { name: /^Analyzing Market Trends/ })).toBeNull();
  });

  it('commits on Enter', () => {
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.change(input, { target: { value: 'Q3 trends' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(props.onRename).toHaveBeenCalledWith('a', 'Q3 trends');
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('commits on blur', () => {
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.change(input, { target: { value: 'Q3 trends' } });
    fireEvent.blur(input);
    expect(props.onRename).toHaveBeenCalledWith('a', 'Q3 trends');
  });

  it('cancels on Escape without renaming', () => {
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.change(input, { target: { value: 'discarded' } });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(props.onRename).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /^Analyzing Market Trends/ })).not.toBeNull();
  });

  it('does not fire a blur commit after an Escape', () => {
    // React does not fire blur on unmount but a browser may on the underlying element; the
    // guard makes the outcome the same either way rather than depending on which.
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.change(input, { target: { value: 'discarded' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.blur(input);
    expect(props.onRename).not.toHaveBeenCalled();
  });

  it('treats an emptied title as a cancel', () => {
    // `RenameConversationRequest.title` is required, and a blank row is indistinguishable from
    // any other blank row.
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(props.onRename).not.toHaveBeenCalled();
  });

  it('does not spend a PATCH on an unchanged title', () => {
    const { props } = list();
    const input = startRename('Analyzing Market Trends');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(props.onRename).not.toHaveBeenCalled();
  });

  it('renames an untitled conversation from its displayed fallback', () => {
    const { props } = list();
    const input = startRename('New chat');
    fireEvent.change(input, { target: { value: 'Named at last' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(props.onRename).toHaveBeenCalledWith('c', 'Named at last');
  });
});

describe('FR-SBR-07 delete', () => {
  const askDelete = (name: string) => {
    const menu = openMenu(name);
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Delete' }));
    return screen.getByRole('dialog');
  };

  it('requires a confirmation before deleting', () => {
    const { props } = list();
    askDelete('Analyzing Market Trends');
    // "delete removes the conversation after a confirmation" — nothing may happen on the menu
    // click itself.
    expect(props.onDelete).not.toHaveBeenCalled();
  });

  it('deletes on confirm', async () => {
    const { props } = list();
    const dialog = askDelete('Analyzing Market Trends');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(props.onDelete).toHaveBeenCalledWith('a');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the dialog open and shows the server copy when the delete is refused', async () => {
    // R-54(5) — the thread purge runs before the commit, so a `503` leaves the chat intact and
    // the retry meaningful. Dismissing the dialog would put the row back in the list with no
    // explanation, and the sidebar has no other surface to give one on.
    const refused = "Couldn't delete this chat just now. Please try again shortly.";
    const { props } = list({ onDelete: vi.fn(async () => refused) });
    const dialog = askDelete('Analyzing Market Trends');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.getByRole('dialog').textContent).toContain(refused));
    expect(props.onDelete).toHaveBeenCalledWith('a');
    // Still offering the action that failed, because trying again is what R-54(5) expects.
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' })).toBeTruthy();
  });

  it('does nothing on cancel', () => {
    const { props } = list();
    const dialog = askDelete('Analyzing Market Trends');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(props.onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does nothing on Escape', () => {
    const { props } = list();
    askDelete('Analyzing Market Trends');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('focuses Cancel, not Delete, so a stray Enter dismisses', () => {
    list();
    const dialog = askDelete('Analyzing Market Trends');
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }));
  });
});
