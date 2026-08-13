/**
 * The token rule, and the cross-language guard that keeps it one rule.
 *
 * R-51(2) sanctions a second implementation of `ceil(len/4)` in the browser because FR-STA-04
 * blocks before a request exists. What it does not sanction is the two drifting: the failure
 * mode is a composer that refuses what the server accepts, or accepts what the server refuses
 * — and the second is an unpredicted `409` (the R-71(1) reconciliation path). So the constant
 * is asserted against
 * `backend/app/tokens.py` **read off disk**, not against a copy of the number.
 */
import { describe, expect, it } from 'vitest';

import { readSource } from './test/css-source';
import { CHARS_PER_TOKEN, estimateTokens, isFrozen, projectSubmission } from './tokens';

describe('CHARS_PER_TOKEN', () => {
  it('equals the backend constant it is a copy of', () => {
    // Reading the Python source rather than trusting a duplicated literal: this is the only
    // check in the repo that can see the two sides part company, and it costs one regex.
    const python = readSource('../backend/app/tokens.py');
    const match = /^CHARS_PER_TOKEN\s*=\s*([\d.]+)$/m.exec(python);
    expect(match, 'CHARS_PER_TOKEN not found in backend/app/tokens.py').not.toBeNull();
    expect(Number(match?.[1])).toBe(CHARS_PER_TOKEN);
  });

  it('is still applied by the backend as ceil(len / CHARS_PER_TOKEN)', () => {
    // The constant matching is not enough — if the backend swapped in a real tokenizer this
    // guard would still pass while the two sides diverged on every string. Pin the shape too.
    const python = readSource('../backend/app/tokens.py');
    expect(python).toMatch(/math\.ceil\(len\(text\)\s*\/\s*CHARS_PER_TOKEN\)/);
  });
});

describe('estimateTokens', () => {
  it('rounds up', () => {
    expect(estimateTokens('')).toBe(0);
    expect(estimateTokens('a')).toBe(1);
    expect(estimateTokens('abcd')).toBe(1);
    expect(estimateTokens('abcde')).toBe(2);
  });

  it('is per message, not per concatenation', () => {
    // `sum(ceil(len_i/4))` != `ceil(sum(len_i)/4)`, and the backend counts the first way
    // (`conversation_usage` sums per message). A composer that counted the other way would
    // disagree with the meter beside it.
    const parts = ['abcde', 'fg'];
    const perMessage = parts.reduce((n, p) => n + estimateTokens(p), 0);
    expect(perMessage).toBe(3);
    expect(estimateTokens(parts.join(''))).toBe(2);
  });
});

describe('projectSubmission', () => {
  const usage = { used: 100, limit: 1000, reserve: 200 };

  it('reserves headroom for the answer', () => {
    // Without the reserve this 700-token query fits; with it, it does not. R-51(3).
    const check = projectSubmission('x'.repeat(2800), usage);
    expect(check.queryTokens).toBe(700);
    expect(check.projectedTokens).toBe(1000);
    expect(check.allowed).toBe(true);
  });

  it('treats a projection landing exactly on the limit as allowed', () => {
    // Ties permissive — `check_submission` uses `<=`. Flipping this to `<` would make the
    // composer refuse the one message the server accepts.
    expect(projectSubmission('x'.repeat(2800), usage).projectedTokens).toBe(usage.limit);
    expect(projectSubmission('x'.repeat(2800), usage).allowed).toBe(true);
    expect(projectSubmission('x'.repeat(2801), usage).allowed).toBe(false);
  });

  it('takes the reserve as a parameter so an operator change cannot desynchronise it', () => {
    const raised = { ...usage, reserve: 400 };
    expect(projectSubmission('x'.repeat(2800), usage).allowed).toBe(true);
    expect(projectSubmission('x'.repeat(2800), raised).allowed).toBe(false);
  });
});

describe('isFrozen', () => {
  it('is false while any message still fits', () => {
    expect(isFrozen({ used: 100, limit: 1000, reserve: 200 })).toBe(false);
  });

  it('is true once even an empty query overflows', () => {
    // R-67(1): the history is not user-reducible, so there is nothing left to shorten and the
    // GUI must present a terminal state rather than invite an edit.
    expect(isFrozen({ used: 900, limit: 1000, reserve: 200 })).toBe(true);
  });

  it('is exactly the boundary at which no draft can be sent', () => {
    const usage = { used: 800, limit: 1000, reserve: 200 };
    expect(isFrozen(usage)).toBe(false);
    expect(projectSubmission('', usage).allowed).toBe(true);
    expect(projectSubmission('a', usage).allowed).toBe(false);
  });
});
