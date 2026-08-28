/**
 * The transcript reducer, driven through the sequences a user actually produces.
 *
 * The cases that earn their place are the ones a plausible implementation gets wrong: a
 * regenerate *failure* must leave the answer, its chips and its rating untouched; a reload must
 * not erase the `TurnOutcome`s the stream taught us; and a notice must stay under its answer
 * across two more re-fetches of the list it is anchored into.
 */
import { describe, expect, it } from 'vitest';

import type { ContextWindow, Message } from '../api';
import {
  EMPTY_CHAT,
  EMPTY_CHAT_STATE,
  LOCAL_ID_PREFIX,
  chatOf,
  chatReducer,
  degradedEntry,
  entriesOf,
  isLocalId,
  pendingEntry,
} from './turns';
import type { ChatAction, ChatState } from './turns';

const CHAT = 'c1';

function ai(id: string, text: string, over: Partial<Message> = {}): Message {
  return {
    id,
    role: 'ai',
    segs: [{ text }],
    created_at: '2026-08-13T09:00:00Z',
    model_name: 'gpt-4o',
    ungrounded: false,
    ungrounded_offerable: false,
    ...over,
  };
}

function user(id: string, text: string): Message {
  return {
    id,
    role: 'user',
    segs: [{ text }],
    created_at: '2026-08-13T08:59:00Z',
    ungrounded: false,
    ungrounded_offerable: false,
  };
}

const USAGE: ContextWindow = {
  used_tokens: 240,
  limit_tokens: 10_400,
  remaining_tokens: 10_160,
  percent_used: 2.3,
  answer_reserve_tokens: 1_500,
};

function run(actions: ChatAction[], from: ChatState = EMPTY_CHAT_STATE): ChatState {
  return actions.reduce(chatReducer, from);
}

const texts = (state: ChatState): string[] =>
  entriesOf(chatOf(state, CHAT)).map((entry) =>
    entry.message.segs.map((seg) => ('text' in seg ? seg.text : '')).join(''),
  );

// --- the send lifecycle -------------------------------------------------------

describe('a send', () => {
  it('shows the user bubble synchronously, with a local id', () => {
    const state = run([{ type: 'asked', conversationId: CHAT, localId: '1', text: 'hello' }]);
    expect(texts(state)).toEqual(['hello']);
    expect(state.turn).toEqual({ conversationId: CHAT, kind: 'send' });
    expect(isLocalId(entriesOf(chatOf(state, CHAT))[0].message.id)).toBe(true);
  });

  it('keeps the user bubble when the answer arrives, and orders them', () => {
    // The user's own row is never on the stream — `record_question` writes it before the first
    // byte — so dropping the bubble on the `message` frame would leave an answer to a question
    // that is not on screen until the reload lands.
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'hello' },
      { type: 'answered', conversationId: CHAT, message: ai('a1', 'hi'), outcome: 'answered' },
    ]);
    expect(texts(state)).toEqual(['hello', 'hi']);
  });

  it('replaces the tail with the server rows once the reload lands', () => {
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'hello' },
      { type: 'answered', conversationId: CHAT, message: ai('a1', 'hi'), outcome: 'answered' },
      { type: 'settled' },
      { type: 'loaded', conversationId: CHAT, messages: [user('u1', 'hello'), ai('a1', 'hi')] },
    ]);
    expect(texts(state)).toEqual(['hello', 'hi']);
    expect(chatOf(state, CHAT).pendingTurn).toEqual([]);
    expect(entriesOf(chatOf(state, CHAT)).every((e) => !isLocalId(e.message.id))).toBe(true);
  });

  it('does NOT drop the tail when a reload lands mid-turn', () => {
    // Reachable: the user switches away and back while the answer is still being written, and
    // the activation reload fires. Dropping the tail there erases the bubble under the dots.
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'hello' },
      { type: 'loaded', conversationId: CHAT, messages: [] },
    ]);
    expect(texts(state)).toEqual(['hello']);
  });

  it('a second turn supersedes the first turn’s local entries', () => {
    // Otherwise a failed send's copy sits at the end of the transcript for the session,
    // drifting below every answer that comes after it.
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'first' },
      {
        type: 'answered',
        conversationId: CHAT,
        message: degradedEntry('it broke').message,
        outcome: 'error',
      },
      { type: 'settled' },
      { type: 'asked', conversationId: CHAT, localId: '2', text: 'second' },
    ]);
    expect(texts(state)).toEqual(['second']);
  });

  it('renders a degraded answer beneath the question that produced it', () => {
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'hello' },
      {
        type: 'answered',
        conversationId: CHAT,
        message: degradedEntry('The model is unavailable.').message,
        outcome: 'error',
      },
    ]);
    expect(texts(state)).toEqual(['hello', 'The model is unavailable.']);
  });
});

// --- regenerate ---------------------------------------------------------------

describe('a regenerate', () => {
  const seeded = (): ChatState =>
    run([
      {
        type: 'loaded',
        conversationId: CHAT,
        messages: [
          user('u1', 'q'),
          ai('a1', 'first answer', { feedback: 'up' }),
          ai('a2', 'later'),
        ],
      },
    ]);

  it('replaces the answer in place, keeping its position', () => {
    const state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: ai('a1', 'second answer'),
          outcome: 'answered',
          targetId: 'a1',
        },
      ],
      seeded(),
    );
    expect(texts(state)).toEqual(['q', 'second answer', 'later']);
  });

  it('never appends — the replacement is the same row', () => {
    const state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: ai('a1', 'second'),
          outcome: 'answered',
          targetId: 'a1',
        },
      ],
      seeded(),
    );
    expect(chatOf(state, CHAT).messages).toHaveLength(3);
  });

  it('a FAILURE leaves the original answer, its feedback and its evaluation intact', () => {
    // R-56: a re-run that errors changes nothing server-side, so the row on screen is still
    // the truth. Replacing it with the error would destroy an answer the user still has.
    const state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: { id: 'a1', role: 'ai', segs: [{ text: 'Something went wrong.' }] },
          outcome: 'error',
          targetId: 'a1',
        },
      ],
      seeded(),
    );
    const rows = chatOf(state, CHAT).messages;
    expect(rows.find((m) => m.id === 'a1')?.feedback).toBe('up');
    expect(texts(state)).toEqual(['q', 'first answer', 'Something went wrong.', 'later']);
  });

  it('anchors that notice by id, so two more reloads cannot move it', () => {
    let state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: { id: 'a1', role: 'ai', segs: [{ text: 'failed' }] },
          outcome: 'error',
          targetId: 'a1',
        },
        { type: 'settled' },
      ],
      seeded(),
    );
    // The post-turn reload, then the eval refresh — with a row inserted ahead of the anchor,
    // which is exactly what an index-based anchor would slide past.
    for (const _ of [0, 1]) {
      state = chatReducer(state, {
        type: 'loaded',
        conversationId: CHAT,
        messages: [
          user('u0', 'earlier'),
          user('u1', 'q'),
          ai('a1', 'first answer'),
          ai('a2', 'later'),
        ],
      });
    }
    expect(texts(state)).toEqual(['earlier', 'q', 'first answer', 'failed', 'later']);
  });

  it('a retry clears the previous attempt’s notice', () => {
    const state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: { id: 'a1', role: 'ai', segs: [{ text: 'failed' }] },
          outcome: 'error',
          targetId: 'a1',
        },
        { type: 'settled' },
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
      ],
      seeded(),
    );
    expect(texts(state)).toEqual(['q', 'first answer', 'later']);
  });

  it('drops a notice whose target no longer exists', () => {
    // Otherwise it can never render again and nothing ever collects it.
    const state = run(
      [
        { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
        {
          type: 'answered',
          conversationId: CHAT,
          message: { id: 'a1', role: 'ai', segs: [{ text: 'failed' }] },
          outcome: 'error',
          targetId: 'a1',
        },
        { type: 'settled' },
        { type: 'loaded', conversationId: CHAT, messages: [user('u1', 'q')] },
      ],
      seeded(),
    );
    expect(chatOf(state, CHAT).notices).toEqual([]);
  });
});

// --- outcomes -----------------------------------------------------------------

describe('outcomes', () => {
  it('survive a reload, because the transcript route does not carry them', () => {
    // They ride the SSE `message` frame only. Keyed by id is what makes a reloaded transcript
    // keep the NFR-A11Y-05 announcement the live turn earned.
    const state = run([
      { type: 'loaded', conversationId: CHAT, messages: [user('u1', 'q')] },
      { type: 'regenerating', conversationId: CHAT, targetId: 'a1' },
    ]);
    const withAnswer = run(
      [
        { type: 'loaded', conversationId: CHAT, messages: [user('u1', 'q'), ai('a1', 'x')] },
        {
          type: 'answered',
          conversationId: CHAT,
          message: ai('a1', 'x'),
          outcome: 'abstained',
          targetId: 'a1',
        },
        { type: 'loaded', conversationId: CHAT, messages: [user('u1', 'q'), ai('a1', 'x')] },
      ],
      state,
    );
    const entry = entriesOf(chatOf(withAnswer, CHAT)).find((e) => e.message.id === 'a1');
    expect(entry?.outcome).toBe('abstained');
  });
});

// --- feedback, usage, deletion ------------------------------------------------

describe('the rest', () => {
  it('feedback replaces the row wherever it lives', () => {
    const seeded = run([{ type: 'loaded', conversationId: CHAT, messages: [ai('a1', 'answer')] }]);
    const rated = chatReducer(seeded, {
      type: 'feedback',
      conversationId: CHAT,
      message: ai('a1', 'answer', { feedback: 'down' }),
    });
    expect(chatOf(rated, CHAT).messages[0].feedback).toBe('down');
  });

  it('feedback also reaches an answer still in this turn’s tail', () => {
    // Reachable: the answer landed on the `message` frame and the user rated it before the
    // reload replaced it with the server's row.
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'q' },
      { type: 'answered', conversationId: CHAT, message: ai('a1', 'answer'), outcome: 'answered' },
      { type: 'feedback', conversationId: CHAT, message: ai('a1', 'answer', { feedback: 'up' }) },
    ]);
    const answered = chatOf(state, CHAT).pendingTurn[1].message;
    expect('created_at' in answered && answered.feedback).toBe('up');
  });

  it('usage is per chat', () => {
    const state = run([{ type: 'usage', conversationId: CHAT, usage: USAGE }]);
    expect(chatOf(state, CHAT).usage).toEqual(USAGE);
    expect(chatOf(state, 'other').usage).toBeNull();
  });

  it('settled clears the turn and is idempotent', () => {
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'q' },
      { type: 'settled' },
    ]);
    expect(state.turn).toBeNull();
    expect(chatReducer(state, { type: 'settled' })).toBe(state);
  });

  it('removing a conversation drops its state and any turn it owned', () => {
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'q' },
      { type: 'removed', conversationId: CHAT },
    ]);
    expect(state.byConversation[CHAT]).toBeUndefined();
    expect(state.turn).toBeNull();
  });

  it('removing one conversation leaves another’s turn alone', () => {
    const state = run([
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'q' },
      { type: 'removed', conversationId: 'other' },
    ]);
    expect(state.turn).toEqual({ conversationId: CHAT, kind: 'send' });
  });

  it('a turn in one chat does not touch another’s rows', () => {
    const state = run([
      { type: 'loaded', conversationId: 'other', messages: [ai('x', 'theirs')] },
      { type: 'asked', conversationId: CHAT, localId: '1', text: 'mine' },
      { type: 'answered', conversationId: CHAT, message: ai('a1', 'answer'), outcome: 'answered' },
    ]);
    expect(entriesOf(chatOf(state, 'other'))).toHaveLength(1);
  });
});

describe('local ids', () => {
  it('are recognisable, and a server id is not one', () => {
    expect(isLocalId(pendingEntry('hi', '3').message.id)).toBe(true);
    expect(pendingEntry('hi', '3').message.id).toBe(`${LOCAL_ID_PREFIX}3`);
    expect(isLocalId('c8f1e2a0-0000-4000-8000-000000000000')).toBe(false);
    expect(isLocalId(null)).toBe(false);
  });
});

describe("the 'ungrounded' action (FR-MSG-09, R-98)", () => {
  it('appends the answer and leaves the abstention in place', () => {
    // R-98(1) leans on the abstention as the record that the corpus could not answer, so this
    // is the deliberate opposite of a regenerate: both rows survive, in order.
    const abstention = ai('a1', 'I could not ground an answer to that.');
    const state = chatReducer(
      { byConversation: { [CHAT]: { ...EMPTY_CHAT, messages: [abstention] } }, turn: null },
      { type: 'ungrounded', conversationId: CHAT, message: ai('a2', 'From training.', {
        ungrounded: true,
      }) },
    );

    const messages = state.byConversation[CHAT].messages;
    expect(messages.map((m) => m.id)).toEqual(['a1', 'a2']);
    expect(messages[1].ungrounded).toBe(true);
  });

  it('is idempotent on the same id', () => {
    // StrictMode double-invokes, and a duplicated id renders the answer twice under one key.
    const message = ai('a2', 'From training.', { ungrounded: true });
    const once = chatReducer(
      { byConversation: { [CHAT]: { ...EMPTY_CHAT, messages: [ai('a1', 'no')] } }, turn: null },
      { type: 'ungrounded', conversationId: CHAT, message },
    );
    const twice = chatReducer(once, { type: 'ungrounded', conversationId: CHAT, message });

    expect(twice.byConversation[CHAT].messages.map((m) => m.id)).toEqual(['a1', 'a2']);
  });

  it('does not claim the turn', () => {
    // `turn` drives the FR-MSG-05 dots, the composer and the KB modal's disabled state. This is
    // one non-streaming call with no stages to report; taking the turn would disable half the
    // product for its duration - the deadlock shape of section 8.58.
    const state = chatReducer(
      { byConversation: { [CHAT]: { ...EMPTY_CHAT, messages: [] } }, turn: null },
      { type: 'ungrounded', conversationId: CHAT, message: ai('a2', 'x', { ungrounded: true }) },
    );

    expect(state.turn).toBeNull();
  });
});
