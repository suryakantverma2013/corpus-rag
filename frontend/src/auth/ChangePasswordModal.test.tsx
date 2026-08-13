/**
 * FR-AUT-09 — the change-password modal.
 *
 * The overlay, focus trap, Escape and focus restore come from `ui/Dialog` and are tested
 * there; what is here is the form, the one local validation, and the boundary between
 * "decidable in the browser" and "the realm's answer".
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AuthContext } from './AuthContext';
import type { AuthContextValue, AuthResult } from './AuthContext';
import { ChangePasswordModal } from './ChangePasswordModal';
import { PASSWORDS_DONT_MATCH } from './copy';

function mount() {
  const changePassword = vi.fn<(a: string, b: string) => Promise<AuthResult>>();
  changePassword.mockResolvedValue({ ok: true });
  const onClose = vi.fn();
  const onChanged = vi.fn();
  const value: AuthContextValue = {
    phase: 'authenticated',
    user: null,
    expired: false,
    signIn: vi.fn(async () => ({ ok: true }) as const),
    signOut: vi.fn(async () => undefined),
    changePassword,
  };
  const result = render(
    <AuthContext value={value}>
      <ChangePasswordModal onClose={onClose} onChanged={onChanged} />
    </AuthContext>,
  );
  return { ...result, changePassword, onClose, onChanged };
}

const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const save = () => screen.getByRole('button', { name: 'Save password' });

function fill(current: string, next: string, confirm: string) {
  fireEvent.change(field('CURRENT PASSWORD'), { target: { value: current } });
  fireEvent.change(field('NEW PASSWORD'), { target: { value: next } });
  fireEvent.change(field('CONFIRM NEW PASSWORD'), { target: { value: confirm } });
}

describe('the form', () => {
  it('is a dialog with the specified title and three password fields', () => {
    mount();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByRole('heading', { name: 'Change password' })).not.toBeNull();
    for (const label of ['CURRENT PASSWORD', 'NEW PASSWORD', 'CONFIRM NEW PASSWORD']) {
      expect(field(label).type).toBe('password');
    }
    // The new-password fields carry `new-password`, not `current-password`: the hint is what
    // stops a password manager filling all three with the existing one.
    expect(field('NEW PASSWORD').getAttribute('autocomplete')).toBe('new-password');
    expect(field('CONFIRM NEW PASSWORD').getAttribute('autocomplete')).toBe('new-password');
    expect(field('CURRENT PASSWORD').getAttribute('autocomplete')).toBe('current-password');
  });

  it('closes from ✕ and from Cancel without calling the API', () => {
    const { onClose, changePassword } = mount();
    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0]);
    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[1]);
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(changePassword).not.toHaveBeenCalled();
  });
});

describe('the mismatch (FR-AUT-09)', () => {
  it('is decided locally and never becomes a request', () => {
    const { changePassword } = mount();
    fill('old', 'new-one', 'new-two');
    fireEvent.click(save());
    expect(screen.getByRole('alert').textContent).toBe(PASSWORDS_DONT_MATCH);
    expect(changePassword).not.toHaveBeenCalled();
    expect(field('CONFIRM NEW PASSWORD').getAttribute('aria-invalid')).toBe('true');
  });

  it('clears once the two agree', async () => {
    const { changePassword } = mount();
    fill('old', 'new-one', 'new-two');
    fireEvent.click(save());
    expect(screen.queryByRole('alert')).not.toBeNull();

    fireEvent.change(field('CONFIRM NEW PASSWORD'), { target: { value: 'new-one' } });
    await act(async () => {
      fireEvent.click(save());
    });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(changePassword).toHaveBeenCalledWith('old', 'new-one');
  });
});

describe('the server’s answer', () => {
  it('renders a rejection inline and keeps the modal open', async () => {
    // A 401 here is "your current password is wrong" — an inline error, NOT the session
    // ending. `client.test.ts` pins the other half: this route's 401 never reaches the
    // return-to-login handler.
    const { changePassword, onClose } = mount();
    changePassword.mockResolvedValue({ ok: false, message: 'Current password is incorrect.' });
    fill('wrong', 'new-one', 'new-one');
    await act(async () => {
      fireEvent.click(save());
    });
    expect(screen.getByRole('alert').textContent).toBe('Current password is incorrect.');
    expect(onClose).not.toHaveBeenCalled();
    // Re-enabled, or the user is stuck looking at an error they cannot act on.
    await waitFor(() => expect(field('CURRENT PASSWORD').disabled).toBe(false));
  });

  it('renders a realm policy rejection verbatim (NFR-SEC-04 lives in Keycloak)', async () => {
    // The modal enforces no policy of its own: the rules are realm configuration (R-28), so a
    // client-side rule set would refuse passwords the server accepts and vice versa.
    const { changePassword } = mount();
    changePassword.mockResolvedValue({ ok: false, message: 'Invalid password: too short.' });
    fill('old', 'x', 'x');
    await act(async () => {
      fireEvent.click(save());
    });
    expect(screen.getByRole('alert').textContent).toBe('Invalid password: too short.');
  });

  it('closes on success and announces it (R-72(5))', async () => {
    const { onClose, onChanged } = mount();
    fill('old', 'new-one', 'new-one');
    await act(async () => {
      fireEvent.click(save());
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    // The closing IS the feedback for a sighted user, and no feedback at all for anyone else —
    // the panel simply disappears with nothing announced.
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('disables the form while saving and will not submit twice', async () => {
    let release!: (value: AuthResult) => void;
    const { changePassword } = mount();
    changePassword.mockReturnValue(new Promise((r) => (release = r)));
    fill('old', 'new-one', 'new-one');

    await act(async () => {
      fireEvent.click(save());
    });
    expect(field('CURRENT PASSWORD').disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Saving…' })).not.toBeNull();

    await act(async () => {
      fireEvent.submit(field('CURRENT PASSWORD').closest('form')!);
    });
    expect(changePassword).toHaveBeenCalledTimes(1);
    await act(async () => release({ ok: true }));
  });
});
