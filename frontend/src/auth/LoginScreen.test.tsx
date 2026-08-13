/**
 * FR-AUT-01..05 — the login screen's behaviour.
 *
 * Rendered against a stub context so each state is reachable directly; the transitions between
 * them belong to `AuthProvider.test.tsx` and the guard to `guard.test.tsx`.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AuthContext } from './AuthContext';
import type { AuthContextValue, AuthResult } from './AuthContext';
import { LoginScreen } from './LoginScreen';
import {
  FORGOT_PASSWORD,
  INVALID_CREDENTIALS,
  LOGIN_SUBTITLE,
  SESSION_EXPIRED,
  TOO_MANY_ATTEMPTS,
} from './copy';

function mount(overrides: Partial<AuthContextValue> = {}) {
  const signIn = vi.fn<(email: string, password: string) => Promise<AuthResult>>();
  signIn.mockResolvedValue({ ok: true });
  const value: AuthContextValue = {
    phase: 'anonymous',
    user: null,
    expired: false,
    signIn,
    signOut: vi.fn(async () => undefined),
    changePassword: vi.fn(async () => ({ ok: true }) as const),
    ...overrides,
  };
  const result = render(
    <AuthContext value={value}>
      <LoginScreen brandName="Corpus" version="v1.4" />
    </AuthContext>,
  );
  return { ...result, signIn: value.signIn as typeof signIn };
}

const email = () => screen.getByLabelText('EMAIL') as HTMLInputElement;
const password = () => screen.getByLabelText('PASSWORD') as HTMLInputElement;
const submit = () => screen.getByRole('button', { name: 'Sign in' });

describe('FR-AUT-01 — the card', () => {
  it('renders the brand block, and the brand is the document’s only <h1>', () => {
    mount();
    const headings = screen.getAllByRole('heading', { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toBe('Corpus');
    expect(screen.getByText(LOGIN_SUBTITLE)).not.toBeNull();
  });

  it('takes the brand name from the FR-SYS-04 prop', () => {
    render(
      <AuthContext
        value={
          {
            phase: 'anonymous',
            user: null,
            expired: false,
            signIn: vi.fn(),
            signOut: vi.fn(),
            changePassword: vi.fn(),
          } as unknown as AuthContextValue
        }
      >
        <LoginScreen brandName="Acme KB" version="v1.4" />
      </AuthContext>,
    );
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Acme KB');
  });
});

describe('FR-AUT-02 — the fields', () => {
  it('autofocuses email and carries the autocomplete hints', () => {
    mount();
    expect(document.activeElement).toBe(email());
    expect(email().getAttribute('autocomplete')).toBe('username');
    expect(password().getAttribute('autocomplete')).toBe('current-password');
    expect(password().type).toBe('password');
  });

  it('toggles Show/Hide, and says so to a screen reader', () => {
    mount();
    const toggle = screen.getByRole('button', { name: 'Show' });
    expect(toggle.getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(toggle);
    expect(password().type).toBe('text');
    const hide = screen.getByRole('button', { name: 'Hide' });
    expect(hide.getAttribute('aria-pressed')).toBe('true');

    fireEvent.click(hide);
    expect(password().type).toBe('password');
  });

  it('puts the toggle between the password field and Sign in (FR-AUT-02’s tab order)', () => {
    mount();
    // Source order IS tab order here — nothing carries a positive tabindex, which is what
    // NFR-A11Y-04's "logical tab order" means in practice.
    const focusable = [...document.querySelectorAll('input, button')];
    expect(focusable.map((el) => el.getAttribute('type'))).toEqual([
      'email',
      'password',
      'button',
      'submit',
    ]);
  });
});

describe('FR-AUT-03 — submitting', () => {
  it('sends the typed credentials', async () => {
    const { signIn } = mount();
    fireEvent.change(email(), { target: { value: 'maya@example.com' } });
    fireEvent.change(password(), { target: { value: 'secret' } });
    await act(async () => {
      fireEvent.click(submit());
    });
    expect(signIn).toHaveBeenCalledWith('maya@example.com', 'secret');
  });

  it('submits on Enter in either field, with no key handler of its own', async () => {
    // A <form> with a submit button already does this, and doing it that way is also what
    // makes browser validation and password managers work.
    const { signIn } = mount();
    await act(async () => {
      fireEvent.submit(email().closest('form')!);
    });
    expect(signIn).toHaveBeenCalledTimes(1);
  });

  it('disables the inputs and replaces the label with the three dots while pending', async () => {
    let release!: (value: AuthResult) => void;
    const { signIn } = mount();
    signIn.mockReturnValue(new Promise((r) => (release = r)));

    await act(async () => {
      fireEvent.click(submit());
    });

    expect(email().disabled).toBe(true);
    expect(password().disabled).toBe(true);
    // The label is gone, so the accessible name has to come from somewhere — a control that
    // loses its name mid-action is announced as "button".
    const button = screen.getByRole('button', { name: 'Signing in…' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.querySelectorAll('.animate-dot-pulse')).toHaveLength(3);

    await act(async () => {
      release({ ok: false, message: INVALID_CREDENTIALS });
    });
  });

  it('will not submit twice while a sign-in is in flight', async () => {
    let release!: (value: AuthResult) => void;
    const { signIn } = mount();
    signIn.mockReturnValue(new Promise((r) => (release = r)));
    const form = email().closest('form')!;

    await act(async () => {
      fireEvent.submit(form);
    });
    await act(async () => {
      fireEvent.submit(form);
    });
    expect(signIn).toHaveBeenCalledTimes(1);
    await act(async () => release({ ok: true }));
  });
});

describe('FR-AUT-04 — errors', () => {
  it('shows the message the provider returned, as an alert', async () => {
    const { signIn } = mount();
    signIn.mockResolvedValue({ ok: false, message: INVALID_CREDENTIALS });
    await act(async () => {
      fireEvent.click(submit());
    });
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe(INVALID_CREDENTIALS);
    // Both fields point at it, so the error is announced with the field rather than only when
    // the user happens to reach the bottom of the card.
    expect(email().getAttribute('aria-describedby')).toBe(alert.id);
    expect(password().getAttribute('aria-describedby')).toBe(alert.id);
    expect(email().getAttribute('aria-invalid')).toBe('true');
  });

  it('re-enables the form so the user can try again', async () => {
    const { signIn } = mount();
    signIn.mockResolvedValue({ ok: false, message: TOO_MANY_ATTEMPTS });
    await act(async () => {
      fireEvent.click(submit());
    });
    await waitFor(() => expect(email().disabled).toBe(false));
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toBeNull();
  });

  it('keeps the message while the user retypes, and clears it on the next attempt', async () => {
    // Clearing on keystroke would remove the sentence the user is reading in order to act on
    // it. It describes the last *attempt*, so the next attempt is when it stops being true.
    const { signIn } = mount();
    signIn.mockResolvedValue({ ok: false, message: INVALID_CREDENTIALS });
    await act(async () => {
      fireEvent.click(submit());
    });
    await screen.findByRole('alert');

    fireEvent.change(password(), { target: { value: 'another try' } });
    expect(screen.queryByRole('alert')).not.toBeNull();

    let release!: (value: AuthResult) => void;
    signIn.mockReturnValue(new Promise((r) => (release = r)));
    await act(async () => {
      fireEvent.click(submit());
    });
    expect(screen.queryByRole('alert')).toBeNull();
    await act(async () => release({ ok: true }));
  });
});

describe('FR-AUT-05/06 — footer and banner', () => {
  it('offers no reset link and no sign-up affordance', () => {
    mount();
    expect(screen.getByText(FORGOT_PASSWORD)).not.toBeNull();
    // FR-USR-09: there is no self-service reset; FR-USR-03: accounts are administrator-created.
    // Both are absences, so they have to be asserted as absences.
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByRole('button', { name: /sign up|create account|forgot/i })).toBeNull();
  });

  it('renders the §9 version tag', () => {
    mount();
    expect(screen.getByText('v1.4')).not.toBeNull();
  });

  it('shows the expiry banner only when the session actually ended', () => {
    mount({ expired: false });
    expect(screen.queryByText(SESSION_EXPIRED)).toBeNull();

    mount({ expired: true });
    const banner = screen.getByText(SESSION_EXPIRED);
    expect(banner.getAttribute('role')).toBe('status');
    // Above the card, per FR-AUT-06.
    expect(banner.compareDocumentPosition(screen.getAllByRole('heading', { level: 1 })[1])).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });
});
