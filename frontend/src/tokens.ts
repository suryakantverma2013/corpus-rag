/**
 * The browser half of the one token-counting rule — `backend/app/tokens.py`, in TypeScript.
 *
 * R-51(2) requires this to exist and requires it to be *this* rule rather than a better one:
 * FR-STA-04 blocks **before** submission, so the GUI cannot ask the server to count every
 * keystroke, and a mismatch between the two sides shows up as the composer refusing a message
 * the server would have accepted (or, worse, accepting one it refuses — an unpredicted `409`,
 * which is the shape OI-31 already records as unspecified).
 *
 * So this is a deliberate second copy of a rule, and the only defensible kind: `ceil(len / 4)`
 * is one line, has no dependency, and cannot drift *silently* because `tokens.test.ts` reads
 * `backend/app/tokens.py` off disk and fails when the two constants part company. An exact
 * tokenizer would be a WASM bundle here and a network fetch there (R-35(7)), and the two
 * implementations would then disagree in ways no test could pin.
 *
 * **Estimate, by ruling.** R-30(2) makes 10.4K a *product* budget an order of magnitude below
 * the model window, so an error here moves where a chat stops — never whether a request works.
 */

/**
 * Characters per token, and it must equal `CHARS_PER_TOKEN` in `backend/app/tokens.py`.
 * Changing it moves the number in front of the user, so it changes in both places in one
 * commit (R-51(2)). `# TBD(§8.4)` on the backend side; the same TBD, not a second one.
 */
export const CHARS_PER_TOKEN = 4.0;

/**
 * Approximate the token count of `text`.
 *
 * Rounds **up**, and is applied **per message** — `sum(ceil(len_i / 4))` and
 * `ceil(sum(len_i) / 4)` are different numbers, and only the per-message form is one this side
 * can reproduce for a lone composer box without knowing the whole transcript.
 */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

/** What `check_submission` returns, minus the parts only the server needs. */
export interface SubmissionCheck {
  /** False when FR-STA-04 must block. */
  allowed: boolean;
  /** `used + query + reserve` — what the turn would cost once answered. */
  projectedTokens: number;
  queryTokens: number;
}

/**
 * FR-STA-04 for one attempted message. Pure, and **identical to `check_submission`**.
 *
 * The projection is `used + query + reserve`, not `used + query`: the reply is part of the
 * conversation the instant it lands, so without the reserve a chat just under the limit
 * accepts a query, answers it, and is over budget with no block ever having fired (R-51(3)).
 *
 * **The comparison is `<=`, not `<`** — a projection landing exactly on the limit is inside
 * it. Ties are permissive, matching `check_submission` verbatim; getting this backwards would
 * make the composer refuse the one message the server accepts.
 *
 * `reserve` is the server's `CONTEXT_ANSWER_RESERVE_TOKENS`, floored at `LLM_MAX_OUTPUT_TOKENS`
 * by a cross-group validator. It is a **parameter rather than a constant here on purpose**: it
 * is an operator setting, so hard-coding 1500 would silently desynchronise this check from the
 * server the moment the answer ceiling is raised.
 */
export function projectSubmission(
  query: string,
  { used, limit, reserve }: { used: number; limit: number; reserve: number },
): SubmissionCheck {
  const queryTokens = estimateTokens(query);
  const projectedTokens = used + queryTokens + reserve;
  return { allowed: projectedTokens <= limit, projectedTokens, queryTokens };
}

/**
 * Whether the conversation is **frozen** in R-67(1)'s sense: no message of any length fits, so
 * the chat is at its terminal state rather than merely refusing this draft.
 *
 * Distinct from `!projectSubmission(...).allowed`, which is per-draft and clears when the user
 * shortens what they typed. FR-STA-04 says "block until content is reduced"; R-67(1) is the
 * ruling that the *history* is not reducible, so once even an empty query overflows there is
 * nothing left to reduce and the GUI must say so instead of inviting an edit.
 */
export function isFrozen(usage: { used: number; limit: number; reserve: number }): boolean {
  return !projectSubmission('', usage).allowed;
}
