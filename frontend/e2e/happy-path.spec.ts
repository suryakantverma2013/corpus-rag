import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * T-603 — the happy path, end to end, against the real stack.
 *
 * login → upload → ask → cite → feedback → regenerate → rename → delete
 *
 * **What this covers that nothing else does.** Every one of these surfaces has unit tests
 * (1,141 of them) and the backend has 1,728, but both halves are mocked at the seam: `vitest`
 * runs against jsdom with `fetch` stubbed, and the backend suite drives ASGI in a rolled-back
 * transaction with fake embeddings and a `DrainingQueue`. Nothing in the repository had ever
 * driven **a browser through the product** — so the assertions here are the ones that only
 * exist once a real user's actions cross a real network into a real worker: that an upload
 * actually finishes ingesting, that an SSE answer actually renders, that a citation chip
 * actually resolves to the passage it came from.
 *
 * It is one continuous journey rather than eight independent tests, because the steps genuinely
 * depend on each other — there is no "regenerate" without an answer, and no answer without an
 * ingested document. `test.step` gives the per-stage reporting that separate tests would,
 * without paying for a fresh login and a fresh ingestion each time.
 *
 * **Costs real money.** `LLM_BACKEND=openai` with `gpt-4o`, so each run spends a router call, a
 * rerank batch, a generation and (if evaluation is on) DeepEval judge calls. That is deliberate:
 * with a fake model the answer cites by construction, and the citation assertion below would be
 * measuring the fixture rather than the product — the same argument §8.65(8) made for row 9.
 *
 * **Retrieval note.** `EMBEDDING_BACKEND=fake` on this box, so the dense arm carries no meaning
 * and the hybrid's **sparse (Postgres FTS) arm** is what finds the passage. Hence `ANCHOR`: a
 * phrase distinctive enough to be found lexically, unique per run so it cannot collide with a
 * document left behind by an earlier run. `tests/scenarios/` uses exactly this device.
 */

const EMAIL = 'admin@corpus.local';
const PASSWORD = process.env.CORPUS_PASSWORD ?? '';

/** Unique per run — see the retrieval note, and FR-KBM-08 below. */
const RUN = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
const ANCHOR = `zarnyx-${RUN}`;
const FILENAME = `corpus-e2e-${RUN}.md`;
const QUESTION = `What is the ${ANCHOR} retention period?`;

/**
 * Markdown rather than a PDF: it needs no library to synthesise, R-34 parses it with a
 * `section` locator, and the bytes are **unique per run**, which matters twice over — FR-KBM-08
 * dedups on checksum, so a fixed fixture would answer `200 duplicate` on the second run and
 * never ingest, and a fixed anchor would let one run's leftovers satisfy the next run's query.
 */
const DOCUMENT = `# Corpus E2E Fixture ${RUN}

## Retention

The ${ANCHOR} retention period is ninety-seven days, after which records are purged.

## Scope

This document exists only to give the T-603 end-to-end run something real to ingest,
retrieve and cite. The phrase ${ANCHOR} appears nowhere else in the corpus.
`;

/** The shell's landmarks — the honest "we are signed in" signal (AppShell.tsx). */
const shellReady = (page: Page): Locator => page.getByRole('navigation', { name: 'Conversations' });

/**
 * Reload and wait for the signed-in shell — the only way to ask the *server* what it believes.
 *
 * **This exists because two mutations survived without it.** The sidebar updates optimistically:
 * `useChat.remove` dispatches `removed` and `App.onDelete` calls `conversations.forget` before
 * anything is confirmed, so a `toHaveCount(0)` immediately after the click passes even when the
 * `DELETE` never reached the backend at all — verified by mutating `deleteConversation` into a
 * no-op and watching this suite stay green. The same is true of the rename's `PATCH`.
 *
 * So every "did it stick?" claim is asserted **after a reload**. The session survives one,
 * because FR-AUT-07's refresh token is an httpOnly cookie rather than anything the page holds.
 */
async function reloadAndWait(page: Page): Promise<void> {
  await page.reload();
  await expect(shellReady(page)).toBeVisible({ timeout: 30_000 });
}

/** The sidebar's conversation list, scoped so row lookups cannot match sidebar chrome. */
const conversationList = (page: Page): Locator => page.getByRole('list', { name: 'CONVERSATIONS' });

test.describe.configure({ mode: 'serial' });

test('the whole journey: sign in, ingest, ask, cite, rate, regenerate, rename, delete', async ({
  page,
}) => {
  // A fresh chat is created during the run and named here so the sidebar row, the rename and
  // the delete all address the same conversation without guessing.
  let title = '';

  await test.step('sign in', async () => {
    await page.goto('/');

    // `phase === 'starting'` renders *nothing* for one round trip (T-509's StrictMode defect),
    // so waiting on the form rather than on any body content is what makes this stable.
    await page.getByLabel('EMAIL').fill(EMAIL);
    await page.getByLabel('PASSWORD').fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(shellReady(page)).toBeVisible({ timeout: 30_000 });
  });

  await test.step('start a new chat', async () => {
    // Scoped to the sidebar: an untitled row renders the literal text "New chat" too, so an
    // unscoped name lookup is ambiguous the moment one exists.
    await page.getByRole('button', { name: 'New chat', exact: true }).first().click();
    await expect(
      page.getByRole('heading', { level: 3, name: 'Ask your knowledge base' }),
    ).toBeVisible();
  });

  await test.step('upload a document', async () => {
    await page.getByRole('button', { name: 'Add documents' }).click();

    const modal = page.getByRole('dialog', { name: 'Knowledge base' });
    await expect(modal).toBeVisible();

    // `[multiple]` distinguishes the drop zone's input from the per-row Replace inputs, which
    // are single-file. The input is deliberately hidden (visually-hidden, aria-hidden) so it is
    // unreachable by role or label — `setInputFiles` drives hidden inputs, which is the
    // sanctioned way to exercise a drop zone without synthesising a DataTransfer.
    await modal.locator('input[type="file"][multiple]').setInputFiles({
      name: FILENAME,
      mimeType: 'text/markdown',
      buffer: Buffer.from(DOCUMENT, 'utf-8'),
    });
  });

  await test.step('wait for it to finish ingesting', async () => {
    const modal = page.getByRole('dialog', { name: 'Knowledge base' });
    const row = modal.getByRole('listitem').filter({ hasText: FILENAME });

    await expect(row).toBeVisible({ timeout: 30_000 });

    // The assertion that only a live stack can make. Reaching `Ready` means the route committed,
    // arq delivered, ClamAV scanned, the parser ran, the chunker ran, embeddings were written and
    // the version swap landed — then the R-41 SSE stream carried the transition back to this row.
    // Generous, because a real ClamAV scan and a real ingestion are not fast; R-41(8) also warns
    // that intermediate stages may never be observed, so this waits for the terminal state only.
    await expect(row.getByText('Ready', { exact: true })).toBeVisible({ timeout: 120_000 });

    await modal.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(modal).toBeHidden();
  });

  await test.step('ask a grounded question', async () => {
    await page.getByLabel('Ask a question').fill(QUESTION);
    await page.getByRole('button', { name: 'Send' }).click();

    // The answer arrives over SSE only after the FR-CIT-06 gate passes server-side (NFR-PRF-02),
    // so there is nothing to see for several seconds. Waiting on the article rather than on the
    // typing dots is deliberate: the dots are entirely aria-hidden and assert nothing.
    const answer = page.getByRole('article').last();
    await expect(answer).toBeVisible({ timeout: 90_000 });
    await expect(answer).toContainText('ninety-seven', { timeout: 90_000 });
  });

  await test.step('the answer cites the document it was grounded in', async () => {
    const answer = page.getByRole('article').last();

    // FR-MSG-04(2)'s source line — the cheap proof that grounding happened at all.
    await expect(answer.getByText(/grounded in \d+ passages?/)).toBeVisible();

    // FR-CIT-01: the chip is a real button whose accessible name is the filename. That it names
    // *this run's* file is the whole point — it proves the citation resolved to the document
    // just ingested rather than to something an earlier run left in the knowledge base.
    const chip = answer.getByRole('button', { name: FILENAME });
    await expect(chip).toBeVisible();

    // FR-CIT-03: hover rather than focus. Both open the card, but headless Chromium never
    // matches `:focus` (R-60(4)), and hover keeps this suite free of that rendering caveat.
    await chip.hover();
    const card = page.getByRole('tooltip');
    await expect(card).toBeVisible();
    // The quote is denormalised into `messages.citations` (R-36(6)), so what the card shows is
    // the passage as retrieved — and it must be from our document.
    await expect(card).toContainText(ANCHOR);
  });

  await test.step('rate the answer', async () => {
    const answer = page.getByRole('article').last();
    const good = answer.getByRole('button', { name: 'Good answer' });

    // The accessible name is `Good answer`, not the 👍 glyph — T-510 found a probe hunting the
    // emoji and it is the contract worth pinning.
    await good.click();
    await expect(good).toHaveAttribute('aria-pressed', 'true');
  });

  await test.step('regenerate the answer', async () => {
    const answer = page.getByRole('article').last();
    await answer.getByRole('button', { name: 'Regenerate' }).click();

    // R-56(5): Regenerate confirms first, because there is no undo. The confirm button's name
    // equals the trigger's, so it must be reached through the dialog or the click is ambiguous.
    const confirm = page.getByRole('dialog', { name: 'Regenerate this answer?' });
    await expect(confirm).toBeVisible();
    await confirm.getByRole('button', { name: 'Regenerate', exact: true }).click();

    // The replacement is written in place (R-56(2)), so the assertion is that an answer is
    // present and grounded again rather than that a new bubble appeared.
    const replaced = page.getByRole('article').last();
    await expect(replaced.getByText(/grounded in \d+ passages?/)).toBeVisible({ timeout: 90_000 });

    // R-56(3): the regenerate clears the previous feedback, so the thumb is no longer pressed.
    await expect(replaced.getByRole('button', { name: 'Good answer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  await test.step('rename the conversation', async () => {
    // A conversation created in this run is untitled, so its row reads the literal `New chat`.
    const row = conversationList(page).getByRole('listitem').first();
    const current = (await row.getByRole('button').first().textContent()) ?? '';

    await row.hover();
    await row.getByRole('button', { name: /^Actions for / }).click();
    await page.getByRole('menuitem', { name: 'Rename' }).click();

    title = `E2E renamed ${RUN}`;
    const editor = row.getByRole('textbox');
    await editor.fill(title);
    await editor.press('Enter');

    // Asserted on the *row*, not on a button matching the title: each row carries two buttons
    // whose accessible names contain it — the select button (`{title} {date} · N messages`) and
    // the actions trigger (`Actions for {title}`) — so a name-based button lookup is ambiguous
    // by construction. `toHaveCount(1)` also says the rename replaced the title rather than
    // adding a second row.
    await expect(
      conversationList(page).getByRole('listitem').filter({ hasText: title }),
    ).toHaveCount(1);
    expect(current).not.toContain(title);

    // The rename is optimistic in the sidebar, so the assertion above would hold even if the
    // PATCH never landed. Reloading is what turns it into a claim about the server.
    await reloadAndWait(page);
    await expect(
      conversationList(page).getByRole('listitem').filter({ hasText: title }),
    ).toHaveCount(1);
  });

  await test.step('delete the conversation', async () => {
    const row = conversationList(page).getByRole('listitem').filter({ hasText: title }).first();

    await row.hover();
    await row.getByRole('button', { name: `Actions for ${title}` }).click();
    await page.getByRole('menuitem', { name: 'Delete' }).click();

    const confirm = page.getByRole('dialog', { name: 'Delete conversation?' });
    await expect(confirm).toBeVisible();
    // `Delete` is also the name of every document row's delete button, so this is dialog-scoped
    // and exact — an unscoped click here would remove a document instead.
    await confirm.getByRole('button', { name: 'Delete', exact: true }).click();

    await expect(
      conversationList(page).getByRole('listitem').filter({ hasText: title }),
    ).toHaveCount(0);

    // And again: the row is forgotten client-side the moment the request is dispatched, so
    // without this a delete that never reached the backend would pass. Mutating
    // `deleteConversation` into a no-op fails *here*, and nowhere earlier.
    await reloadAndWait(page);
    await expect(
      conversationList(page).getByRole('listitem').filter({ hasText: title }),
    ).toHaveCount(0);
  });

  await test.step('clean up the uploaded document', async () => {
    // Not part of the FR flow, but a run that leaves its fixture behind makes the *next* run's
    // knowledge base a little less like a fresh one — and `fidelity/`'s `selectAnsweredChat`
    // exists because exactly that kind of residue broke a later run.
    await page.getByRole('button', { name: /^Knowledge base/ }).click();
    const modal = page.getByRole('dialog', { name: 'Knowledge base' });
    await modal.getByRole('button', { name: `Delete ${FILENAME}` }).click();

    const confirm = page.getByRole('dialog', { name: 'Delete this document?' });
    await confirm.getByRole('button', { name: 'Delete', exact: true }).click();

    await expect(modal.getByRole('listitem').filter({ hasText: FILENAME })).toHaveCount(0, {
      timeout: 30_000,
    });
  });
});
