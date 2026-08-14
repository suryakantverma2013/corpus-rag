import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Sidebar } from './Sidebar';
import type { SidebarConversation } from './conversations';

const CONVERSATIONS: SidebarConversation[] = [
  {
    id: 'a',
    title: 'Analyzing Market Trends',
    archived: false,
    created_at: '2026-07-16T09:12:00Z',
    updated_at: '2026-07-16T09:12:00Z',
    message_count: 2,
  },
];

function sidebar(overrides: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  const props = {
    brandName: 'Corpus',
    conversations: CONVERSATIONS,
    activeId: 'a',
    onSelect: vi.fn(),
    onNewChat: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
    documentCount: 5,
    onOpenKnowledgeBase: vi.fn(),
    user: { initials: 'MJ', name: 'Maya Jensen', version: 'v1.4' },
    ...overrides,
  };
  return { ...render(<Sidebar {...props} />), props };
}

describe('FR-SBR-01 brand row', () => {
  it('renders the brandName and the mono RAG badge', () => {
    sidebar({ brandName: 'Acme KB' });
    expect(screen.getByText('Acme KB')).not.toBeNull();
    expect(screen.getByText('RAG')).not.toBeNull();
  });

  it('is not a heading — the shell owns the document’s only h1 (T-502)', () => {
    // The brand row's text is the same string as that <h1>, so marking it up as a heading
    // would put the brand in the outline twice.
    sidebar();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });

  it('hides the decorative "C" mark from assistive technology', () => {
    const { container } = sidebar();
    expect(container.querySelector('[aria-hidden="true"]')?.textContent).toBe('C');
  });
});

describe('FR-SBR-02 new chat', () => {
  it('is a button that reports the click', () => {
    const { props } = sidebar();
    fireEvent.click(screen.getByRole('button', { name: 'New chat' }));
    expect(props.onNewChat).toHaveBeenCalledTimes(1);
  });
});

describe('FR-SBR-05 knowledge base', () => {
  it('renders the document count and opens the modal', () => {
    const { props } = sidebar({ documentCount: 12 });
    const button = screen.getByRole('button', { name: /Knowledge base/ });
    expect(button.textContent).toContain('12');
    fireEvent.click(button);
    expect(props.onOpenKnowledgeBase).toHaveBeenCalledTimes(1);
  });
});

describe('FR-SBR-06 user row', () => {
  it('renders the name and version tag', () => {
    sidebar();
    expect(screen.getByText('Maya Jensen')).not.toBeNull();
    expect(screen.getByText('v1.4')).not.toBeNull();
  });

  it('is NOT interactive until T-509 supplies the FR-AUT-08 popover', () => {
    // FR-SBR-06 records the row's interactivity as a §4.17 carve-out, and §4.17 is T-509's.
    // A <button> here today would look live and do nothing — worse than the prototype's
    // non-interactive row, which is what this renders instead.
    sidebar();
    expect(screen.queryByRole('button', { name: /Maya Jensen/ })).toBeNull();
  });

  it('becomes a button, with expansion state, once a handler is supplied', () => {
    const onToggleUserMenu = vi.fn();
    sidebar({ onToggleUserMenu, userMenuOpen: true });
    const button = screen.getByRole('button', { name: /Maya Jensen/ });
    expect(button.getAttribute('aria-haspopup')).toBe('menu');
    expect(button.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(button);
    expect(onToggleUserMenu).toHaveBeenCalledTimes(1);
  });

  it('does not announce the avatar initials, which duplicate the name', () => {
    sidebar();
    // "M J Maya Jensen" is noise; the initials are decorative beside the name they abbreviate.
    expect(screen.queryByText('MJ')?.getAttribute('aria-hidden')).toBe('true');
  });
});

describe('composition', () => {
  it('renders the conversation list', () => {
    sidebar();
    expect(screen.getByRole('heading', { level: 2, name: 'CONVERSATIONS' })).not.toBeNull();
    expect(screen.getByRole('button', { name: /^Analyzing Market Trends/ })).not.toBeNull();
  });
});
