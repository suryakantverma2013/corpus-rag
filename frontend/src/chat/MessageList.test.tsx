/**
 * §4.3 behaviour: FR-MSG-01's four scroll triggers, FR-MSG-02, FR-MSG-05, FR-STA-04's notice and
 * NFR-A11Y-05's live region.
 *
 * jsdom applies no external CSS and lays nothing out, so everything asserted here is DOM and
 * script behaviour. The pixels are T-510's and the reduced-motion rendering is T-511's.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { RenderResult } from '@testing-library/react';
import { CitationHoverProvider } from './CitationHoverProvider';
import { MessageList } from './MessageList';
import type { MessageListProps } from './MessageList';
import type { TranscriptEntry } from './messages';
import type { Message, Segment } from '../api';

let seq = 0;

function user(text: string): TranscriptEntry {
  seq += 1;
  return {
    message: {
      id: `u${seq}`,
      role: 'user',
      segs: [{ text }],
      created_at: 'x',
      ungrounded: false,
      ungrounded_offerable: false,
    },
  };
}

function ai(segs: Segment[], extra: Partial<Message> = {}): TranscriptEntry {
  seq += 1;
  return {
    message: {
      id: `a${seq}`,
      role: 'ai',
      segs,
      created_at: 'x',
      ungrounded: false,
      ungrounded_offerable: false,
      ...extra,
    },
    outcome: 'answered',
  };
}

function list(props: Partial<MessageListProps> = {}): RenderResult {
  return render(
    <CitationHoverProvider>
      <MessageList
        entries={[]}
        typing={false}
        conversationId="c1"
        frozen={false}
        userInitials="MJ"
        onFeedback={() => {}}
        onRegenerate={() => {}}
        onAnswerUngrounded={() => {}}
        ungroundedBusy={new Set()}
        {...props}
      />
    </CitationHoverProvider>,
  );
}

/** The scroll container is the provider's only child. jsdom stores `scrollTop` writes verbatim
 *  and lets `scrollHeight` be defined per element, so FR-MSG-01 is genuinely testable here. */
function scroller(container: HTMLElement): HTMLElement {
  const element = container.firstElementChild as HTMLElement;
  Object.defineProperty(element, 'scrollHeight', { value: 1234, configurable: true });
  return element;
}

beforeEach(() => {
  seq = 0;
});

describe('FR-MSG-01 — auto-scroll', () => {
  it('scrolls to the bottom on initial render', () => {
    // The element does not exist before mount, so `scrollHeight` is stubbed on the prototype —
    // otherwise this trigger is the one of the four that cannot be observed.
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight');
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      value: 999,
      configurable: true,
    });
    try {
      const { container } = list({ entries: [user('hi')] });
      expect((container.firstElementChild as HTMLElement).scrollTop).toBe(999);
    } finally {
      if (original === undefined) {
        Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight');
      } else {
        Object.defineProperty(HTMLElement.prototype, 'scrollHeight', original);
      }
    }
  });

  it('scrolls when a message is added', () => {
    const entries = [user('one')];
    const { container, rerender } = list({ entries });
    const element = scroller(container);
    element.scrollTop = 0;
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={[...entries, ai([{ text: 'answer' }])]}
          typing={false}
          conversationId="c1"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(element.scrollTop).toBe(1234);
  });

  it('scrolls when the typing indicator appears', () => {
    const entries = [user('one')];
    const { container, rerender } = list({ entries });
    const element = scroller(container);
    element.scrollTop = 0;
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={entries}
          typing
          conversationId="c1"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(element.scrollTop).toBe(1234);
  });

  it('scrolls when the chat is selected — a normative Rev 0.5.1 clarification', () => {
    // The prototype scrolls only on message-count and typing changes; FR-MSG-01 adds this one.
    const entries = [user('one')];
    const { container, rerender } = list({ entries });
    const element = scroller(container);
    element.scrollTop = 0;
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={entries}
          typing={false}
          conversationId="c2"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(element.scrollTop).toBe(1234);
  });

  it('does NOT scroll when only a feedback value changed', () => {
    // A thumb click replaces the array identity without adding a row. FR-MSG-01 lists "when a
    // message is added", and yanking the viewport under someone who just rated an answer they
    // are reading is the defect a naive `[entries]` dependency ships.
    const answer = ai([{ text: 'answer' }]);
    const entries = [user('one'), answer];
    const { container, rerender } = list({ entries });
    const element = scroller(container);
    element.scrollTop = 0;
    const rated: TranscriptEntry = {
      ...answer,
      message: { ...(answer.message as Message), feedback: 'up' },
    };
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={[entries[0], rated]}
          typing={false}
          conversationId="c1"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(element.scrollTop).toBe(0);
  });
});

describe('FR-MSG-02 — empty state', () => {
  it('shows when there are no messages and nothing is pending', () => {
    list();
    expect(screen.queryByText('Ask your knowledge base')).not.toBeNull();
  });

  it('hides as soon as there is a message', () => {
    list({ entries: [user('hi')] });
    expect(screen.queryByText('Ask your knowledge base')).toBeNull();
  });

  it('does NOT show while a reply is pending on an empty chat', () => {
    // The prototype's own `isEmpty` is `messages.length === 0 && !typing`: the first question of
    // a new chat would otherwise show the invitation and the typing dots at once.
    list({ typing: true });
    expect(screen.queryByText('Ask your knowledge base')).toBeNull();
  });

  it('names the + button, not the removed paperclip (FR-MSG-02 as amended)', () => {
    list();
    const helper = screen.getByText(/Answers are grounded in your documents/);
    expect(helper.textContent).toContain('the + button');
    expect(helper.textContent).not.toContain('paperclip');
  });

  it('titles itself below the FR-HDR-01 h2, not at the same level', () => {
    // T-502 owns the document's only <h1> and T-504's chat title is the <h2> inside <main>. A
    // second <h2> here puts two peers in the outline for one region — and breaks App.test.tsx,
    // whose delete-everything case ends in exactly this state.
    list();
    expect(screen.getByRole('heading', { name: 'Ask your knowledge base' }).tagName).toBe('H3');
  });
});

describe('FR-MSG-05 — typing indicator', () => {
  it('renders three dots while a reply is pending', () => {
    const { container } = list({ typing: true, entries: [user('hi')] });
    // The dots are decorative to assistive technology — the live region carries the state — so
    // they are queried structurally rather than by role.
    expect(container.querySelectorAll('[aria-hidden="true"] > span')).toHaveLength(3);
  });

  it('renders nothing when no reply is pending', () => {
    const { container } = list({ entries: [user('hi')] });
    expect(container.querySelectorAll('[aria-hidden="true"] > span')).toHaveLength(0);
  });
});

describe('FR-STA-04 / R-67 — the frozen conversation', () => {
  const COPY =
    'This conversation has reached its length limit. Start a new chat to keep going — your ' +
    'documents and their answers stay where they are.';

  it('renders the requirement’s copy verbatim', () => {
    list({ frozen: true, entries: [user('hi')] });
    expect(screen.queryByText(COPY)).not.toBeNull();
  });

  it('names no number (R-51(5))', () => {
    list({ frozen: true, entries: [user('hi')] });
    expect(screen.getByText(COPY).textContent).not.toMatch(/\d/);
  });

  it('is a status, never an alert — frozen is not broken', () => {
    // R-67(1): a designed terminal state. `role="alert"` is assertive and would interrupt the
    // user to announce that nothing has gone wrong.
    list({ frozen: true, entries: [user('hi')] });
    expect(screen.getByText(COPY).getAttribute('role')).toBe('status');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('leaves the transcript readable and its answers still actionable', () => {
    // The load-bearing half: everything except submission still works, so a user does not
    // conclude the chat is lost and delete it.
    list({ frozen: true, entries: [user('hi'), ai([{ text: 'an answer' }])] });
    expect(screen.queryByText('an answer')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Regenerate' })).not.toBeNull();
  });
});

describe('NFR-A11Y-05 — the live region', () => {
  const region = () => document.querySelector('[aria-live]') as HTMLElement;

  it('is polite, never assertive', () => {
    list();
    expect(region().getAttribute('aria-live')).toBe('polite');
  });

  it('announces an answer’s arrival', () => {
    // R-54(2) streams progress frames with no content and commits the answer in one go, so
    // without this a non-sighted user gets no signal that a reply exists.
    const entries = [user('hi')];
    const { rerender } = list({ entries });
    expect(region().textContent).toBe('');
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={[...entries, ai([{ text: 'the answer' }])]}
          typing={false}
          conversationId="c1"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(region().textContent).toBe('Answer received.');
  });

  it('announces a degraded turn’s server copy verbatim (R-43(6))', () => {
    const failure = 'The service is busy right now — wait a moment and try again.';
    list({
      entries: [
        user('hi'),
        { message: { id: null, role: 'ai', segs: [{ text: failure }] }, outcome: 'error' },
      ],
    });
    expect(region().textContent).toBe(failure);
  });

  it('does not re-announce unchanged content on a re-render', () => {
    // The requirement's own clause. A region whose text is reassigned every render repeats
    // itself on every feedback click.
    const answer = ai([{ text: 'the answer' }]);
    const entries = [user('hi'), answer];
    const { rerender } = list({ entries });
    const spy = vi.spyOn(region(), 'textContent', 'set');
    rerender(
      <CitationHoverProvider>
        <MessageList
          entries={[
            entries[0],
            { ...answer, message: { ...(answer.message as Message), feedback: 'up' } },
          ]}
          typing={false}
          conversationId="c1"
          frozen={false}
          userInitials="MJ"
          onFeedback={() => {}}
          onRegenerate={() => {}}
          onAnswerUngrounded={() => {}}
          ungroundedBusy={new Set()}
        />
      </CitationHoverProvider>,
    );
    expect(spy).not.toHaveBeenCalled();
    expect(region().textContent).toBe('Answer received.');
  });
});

describe('R-54(3) — a degraded turn has no row to act on', () => {
  it('renders the server copy with no action bar', () => {
    list({
      entries: [
        user('hi'),
        {
          message: { id: null, role: 'ai', segs: [{ text: 'System Failure.' }] },
          outcome: 'error',
        },
      ],
    });
    // Twice, correctly: once in the bubble and once in the NFR-A11Y-05 live region, which
    // announces the server's own copy rather than a generic phrase.
    expect(screen.getAllByText('System Failure.')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Regenerate' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Good answer' })).toBeNull();
  });

  it('treats an abstention as an ordinary, actionable message', () => {
    // `_should_persist` excludes only `error`, so abstained and injection-blocked turns get a
    // real row and must not be mistaken for failures.
    list({ entries: [user('hi'), ai([{ text: "I couldn't ground an answer to that." }])] });
    expect(screen.queryByRole('button', { name: 'Regenerate' })).not.toBeNull();
  });
});
