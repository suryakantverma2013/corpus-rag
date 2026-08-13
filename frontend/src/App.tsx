/**
 * Corpus GUI root. Owns the FR-SYS-04 configurable-prop boundary, mounts the theme runtime
 * (§4.8) and composes the FR-LAY-01 shell inside it (R-58(5)).
 *
 * This is the composition root: it is where the FR-SYS-04 defaults resolve, where T-503..T-508
 * fill the shell's slots, and where T-509 branches between the §4.17 login screen and the
 * shell.
 *
 * **The FR-CST-01 state below is local and seeded.** T-513 owns wiring the generated client
 * into every view; until then the sidebar is driven from `useState` so its behaviour is
 * buildable and testable. Every mutation here is the shape of the API call that replaces it —
 * `onRename` is `PATCH /conversations/{id}`, `onDelete` is `DELETE /conversations/{id}`.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ThemeProvider } from './theme/ThemeProvider';
import { AuthProvider } from './auth/AuthProvider';
import { ChangePasswordModal } from './auth/ChangePasswordModal';
import { LoginScreen } from './auth/LoginScreen';
import { UserMenu } from './auth/UserMenu';
import { PASSWORD_CHANGED } from './auth/copy';
import { displayName, initials } from './auth/identity';
import { useAuth } from './auth/useAuth';
import { AppShell } from './shell/AppShell';
import { Sidebar } from './sidebar/Sidebar';
import { ChatHeader } from './chat/ChatHeader';
import { CitationCard } from './chat/CitationCard';
import { CitationHoverProvider } from './chat/CitationHoverProvider';
import { MessageList } from './chat/MessageList';
import { SAMPLE_TRANSCRIPTS, asked, regenerated } from './chat/sampleTranscripts';
import type { SampleChat } from './chat/sampleTranscripts';
import { Composer } from './composer/Composer';
import { KnowledgeBaseModal } from './kb/KnowledgeBaseModal';
import { documentCount, toMentionDocuments } from './kb/documents';
import { useDocuments } from './kb/useDocuments';
import { StatsPanel } from './stats/StatsPanel';
import { modelNameOf } from './stats/stats';
import { displayTitle, nextActiveId, UNTITLED_CONVERSATION } from './sidebar/conversations';
import type { SidebarConversation } from './sidebar/conversations';
import type { Feedback, Message } from './api';

/**
 * FR-SYS-04 / §9. The only place this literal exists on the JS side.
 *
 * Resolved here rather than in `AppShell` because `brandName` has consumers on both sides of
 * the shell boundary — FR-SBR-01's brand row inside it, and T-509's FR-AUT-01 login card,
 * which replaces the shell entirely rather than living within it.
 */
const DEFAULT_BRAND_NAME = 'Corpus';

/** The §9/Appendix A GUI version tag, on FR-SBR-06's sidebar row and FR-AUT-05's login footer.
 *  Product metadata, not session data — it is the same string signed in or out. */
const GUI_VERSION = 'v1.4';

/** Sample conversations, standing in for `GET /api/v1/conversations` until T-513. Shaped as
 *  the generated `Conversation` model so the swap is a data-source change, not a type change. */
const SAMPLE_CONVERSATIONS: SidebarConversation[] = [
  seed('Analyzing Market Trends', '2026-07-16T09:12:00Z', 2),
  seed('Product Launch Strategy', '2026-07-14T16:40:00Z', 4),
  seed('Customer Persona Refinement', '2026-07-11T11:05:00Z', 0),
  seed('Pricing Experiment Review', '2026-07-08T08:30:00Z', 4),
  // Added by T-505 so the §4.3 states that only exist in a live turn — the FR-MSG-05 indicator
  // and FR-STA-04's frozen conversation — are reachable before T-513 wires the API.
  seed('Q4 Forecast Draft', '2026-07-06T15:20:00Z', 1),
  seed('Vendor Security Review', '2026-07-02T10:00:00Z', 4),
];

/** A conversation with no seeded transcript — every chat created by FR-SBR-02's New chat — shows
 *  the FR-MSG-02 empty state, which is exactly what a new chat should show. */
const EMPTY_CHAT: SampleChat = { entries: [], typing: false, frozen: false };

/**
 * Seeded FR-ANL-03 usage for the FR-STA-04 projection, standing in for `ContextWindowResponse`.
 *
 * **The derivation runs backwards here on purpose, and T-513 reverses it.** Real usage comes
 * from the server and `frozen` is derived from it (`isFrozen`); until then the seed carries
 * `frozen` and the usage is derived from *that*, so the composer's block and T-505's frozen
 * notice cannot disagree in the meantime. `8_900 + 1_500 = 10_400` is exactly the boundary at
 * which no message of any length fits (R-67(1)).
 */
const ROOMY_USAGE = { used: 240, limit: 10_400, reserve: 1_500 };
const FULL_USAGE = { used: 8_900, limit: 10_400, reserve: 1_500 };

/**
 * FR-ANL-02 / FR-SYS-03 — the configured chat model, standing in for the API until T-513.
 *
 * **No route carries it.** `MeResponse` has no model field and the only model string on the wire
 * is `MessageResponse.model_name`, which is per answer — so a chat with no AI turn yet has
 * nothing to read. This is the backend default (`OpenAISettings.chat_model`), and the seeded
 * answers in `sampleTranscripts.ts` carry the same id, which is why the card does not change when
 * the user opens a conversation that has been answered. Flagged for T-513 alongside
 * `answer_reserve_tokens`.
 */
const SAMPLE_MODEL_NAME = 'gpt-4o';

/**
 * Replace one AI message wherever it lives, leaving every other conversation untouched.
 *
 * Keyed on the message rather than the conversation because both routes address a message id and
 * carry no conversation — T-513 will hand this the route's own `200` body instead.
 */
function patchMessage(
  transcripts: Record<string, SampleChat>,
  messageId: string,
  patch: (message: Message) => Message,
): Record<string, SampleChat> {
  const next: Record<string, SampleChat> = {};
  for (const [id, chat] of Object.entries(transcripts)) {
    next[id] = {
      ...chat,
      entries: chat.entries.map((entry) =>
        entry.message.id === messageId &&
        entry.message.role === 'ai' &&
        'created_at' in entry.message
          ? { ...entry, message: patch(entry.message) }
          : entry,
      ),
    };
  }
  return next;
}

function seed(title: string, updatedAt: string, messageCount: number): SidebarConversation {
  return {
    id: `sample-${title.toLowerCase().replaceAll(' ', '-')}`,
    title,
    archived: false,
    created_at: updatedAt,
    updated_at: updatedAt,
    messageCount,
  };
}

export interface AppProps {
  /**
   * FR-SYS-04 / FR-THM-03 — overrides `--accent` in both themes.
   *
   * Intentionally has NO default value here. FR-SYS-04's default `#7C86F8` is carried by the
   * `--accent` token in `src/styles/tokens.css`, per theme; defaulting it in JS would write
   * the dark accent onto the light theme and destroy the `#5B66E8` NFR-VIS-02 specifies for
   * it. See R-58(2).
   */
  accent?: string;
  /** FR-SYS-04 — §9 default "Corpus". Rendered as the document's <h1> and in the FR-SBR-01
   *  brand row. */
  brandName?: string;
  /** FR-SYS-04 / FR-LAY-02 — §9 default `true`. `false` unmounts the stats panel and the chat
   *  column expands into its track. */
  showStats?: boolean;
}

/**
 * The FR-SYS-04 boundary and the two runtimes every surface sits inside.
 *
 * `AuthProvider` is inside `ThemeProvider`, not beside it: FR-AUT-01 puts the login screen on
 * `--bg` with the stored theme applied, so the theme has to be in force on the *un*authenticated
 * side too. The reverse nesting would leave the login card unthemed.
 */
function App({ accent, brandName = DEFAULT_BRAND_NAME, showStats = true }: AppProps) {
  return (
    <ThemeProvider accent={accent}>
      <AuthProvider>
        <Corpus brandName={brandName} showStats={showStats} />
      </AuthProvider>
    </ThemeProvider>
  );
}

function Corpus({ brandName, showStats }: { brandName: string; showStats: boolean }) {
  const { phase, user, signOut } = useAuth();
  /** FR-AUT-08's popover and FR-AUT-09's modal. Both are session UI, so they live here rather
   *  than in the FR-CST-01 bag — the same reasoning R-58(5) applied to the theme. */
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  /** NFR-A11Y-05 — R-72(5)'s success announcement for a change the modal reports by vanishing. */
  const [announcement, setAnnouncement] = useState('');
  const [conversations, setConversations] = useState<SidebarConversation[]>(SAMPLE_CONVERSATIONS);
  const [activeId, setActiveId] = useState<string | null>(SAMPLE_CONVERSATIONS[0]?.id ?? null);
  const [transcripts, setTranscripts] = useState<Record<string, SampleChat>>(SAMPLE_TRANSCRIPTS);
  const nextId = useRef(0);
  /**
   * FR-CST-01's session `startTime`, driving FR-ANL-01's DURATION.
   *
   * Owned here rather than in `StatsPanel` because FR-LAY-02's `showStats={false}` **unmounts**
   * the panel, so a panel-owned start would restart the clock whenever the column was hidden and
   * shown again. R-14 scopes the duration to the session; only a reload begins a new one.
   */
  const [sessionStartedAt] = useState(() => Date.now());
  /** FR-CST-01's `docsOpen`. Both FR-SBR-05 and FR-CMP-02 open the same modal (FR-KBM-01). */
  const [docsOpen, setDocsOpen] = useState(false);

  /**
   * `POST /api/v1/messages/{id}/feedback` with `{"feedback": "up" | "down" | null}` — the key is
   * required and `null` is FR-MSG-06's third state, which is how the toggle clears. The route
   * answers `200` with the full message, so replacing the row locally is the same shape.
   */
  const onFeedback = useCallback((messageId: string, feedback: Feedback | null) => {
    setTranscripts((current) => patchMessage(current, messageId, (m) => ({ ...m, feedback })));
  }, []);

  /**
   * `POST /api/v1/messages/{id}/regenerate` — an SSE stream identical in shape to a send (T-513
   * consumes it). R-56(2)/(3): the row is replaced **in place**, keeping its id, and `evaluation`
   * and `feedback` are cleared in the same write.
   */
  const onRegenerate = useCallback((messageId: string) => {
    setTranscripts((current) => patchMessage(current, messageId, regenerated));
  }, []);

  /**
   * FR-CMP-03 — append the user's turn and show the FR-MSG-05 indicator.
   *
   * `POST /api/v1/conversations/{id}/messages`, an SSE stream (T-513 consumes it). **The
   * indicator does not clear**, because nothing answers yet: T-505 deliberately seeds no timer
   * and no simulated stream ("the seed models the *effect* of a call"), and inventing a canned
   * reply here would be a second fake backend for T-513 to delete. So this is the honest
   * pre-wiring state — the request was made and nothing has replied — and FR-CMP-03's "shows
   * the typing indicator until the response arrives" is satisfied as literally as it can be.
   */
  const onSend = useCallback(
    (text: string) => {
      if (activeId === null) return;
      setTranscripts((current) => {
        const chat = current[activeId] ?? EMPTY_CHAT;
        return {
          ...current,
          [activeId]: { ...chat, entries: [...chat.entries, asked(text)], typing: true },
        };
      });
    },
    [activeId],
  );

  // FR-SBR-02 — prepends an empty chat and activates it, showing the empty state.
  const onNewChat = useCallback(() => {
    const created = seed(UNTITLED_CONVERSATION, new Date().toISOString(), 0);
    // `seed` derives its id from the title, so every "New chat" would collide. A counter
    // rather than `crypto.randomUUID()`: that is undefined outside a secure context, so it
    // would throw on an HTTP LAN deployment. The real id comes from
    // `POST /api/v1/conversations` in T-513.
    nextId.current += 1;
    const conversation = { ...created, id: `new-${nextId.current}` };
    setConversations((current) => [conversation, ...current]);
    setActiveId(conversation.id);
  }, []);

  const onRename = useCallback((id: string, title: string) => {
    setConversations((current) => current.map((c) => (c.id === id ? { ...c, title } : c)));
  }, []);

  // FR-SBR-07 — "if it was active, selects the next conversation or the empty state".
  // Two separate updaters, deliberately: calling `setActiveId` inside the `setConversations`
  // updater would be a side effect in a function StrictMode invokes twice.
  const onDelete = useCallback(
    (id: string) => {
      setActiveId((active) => (active === id ? nextActiveId(conversations, id) : active));
      setConversations((current) => current.filter((c) => c.id !== id));
    },
    [conversations],
  );

  // FR-HDR-01. `undefined` once the last conversation is deleted (FR-SBR-07 leaves `activeId`
  // null), which `ChatHeader` renders as the untitled-chat label — see `ChatHeaderProps.title`.
  const activeConversation = conversations.find((c) => c.id === activeId);

  // A chat with no seeded transcript — every FR-SBR-02 New chat — shows the empty state.
  const chat = (activeId === null ? undefined : transcripts[activeId]) ?? EMPTY_CHAT;

  // ONE expression, two consumers: the FR-ANL-03 meter and the composer's FR-STA-04 projection
  // read the same numbers, so the panel cannot say a chat has room while the composer refuses it.
  const usage = chat.frozen ? FULL_USAGE : ROOMY_USAGE;

  /**
   * OI-31 / OI-36 (R-71(1)) — the client-side "a turn is in flight" signal, in ONE place.
   *
   * FR-MSG-05's typing indicator, FR-MSG-08's action bar and FR-KBM-07's four gated verbs are the
   * same fact rendered three times, and R-43(4)'s server `409` is a fourth copy that can disagree
   * with all of them. Naming it once here is what stops the modal inventing a second signal;
   * T-513 replaces the right-hand side with the real chat stream's state and touches nothing else.
   */
  const turnInFlight = chat.typing;

  /**
   * The FR-KBM-* store. It owns `GET /documents`, the R-41 event stream and the four verbs —
   * T-508 wires its own surface, which R-69(5) records as this project's convention.
   *
   * **The seeded document list is gone.** FR-CMP-06 requires the composer footer and FR-SBR-05's
   * button to show the *same* number, and until T-509 supplies a token this is legitimately zero
   * rather than a plausible five: a second source that happens to look right is exactly how the
   * two stop agreeing.
   */
  // `enabled` rather than a conditional call: hooks cannot be conditional, so this runs during
  // the `starting` phase too — and an unauthenticated `GET /documents` there answers 401, which
  // FR-AUT-07's handler would read as "the session ended", signing out the very user whose
  // session was in the middle of resuming.
  const documents = useDocuments({
    open: docsOpen,
    conversationId: activeId,
    turnInFlight,
    enabled: phase === 'authenticated',
  });
  const mentionDocuments = toMentionDocuments(documents.rows, activeId);
  const knowledgeBaseCount = documentCount(documents.rows, activeId);

  const openKnowledgeBase = useCallback(() => setDocsOpen(true), []);

  /**
   * FR-AUT-06's (D), resolved by R-72(3): the conversation the user was in **is** restored
   * after re-login — but only for the same user.
   *
   * Restoring is the default rather than the work, because this component stays mounted across
   * the session drop (only what it *returns* changes), so `activeId` and the transcripts are
   * still here. The work is the reset, and it is the half that matters: without it, a colleague
   * signing in on the expiry screen would land in the previous user's chat pointer — which
   * `404`s under R-54(1), but should never be attempted.
   */
  const lastUserId = useRef<string | null>(null);
  useEffect(() => {
    if (user === null) return;
    const previous = lastUserId.current;
    lastUserId.current = user.id;
    if (previous === null || previous === user.id) return;
    setConversations(SAMPLE_CONVERSATIONS);
    setActiveId(SAMPLE_CONVERSATIONS[0]?.id ?? null);
    setTranscripts(SAMPLE_TRANSCRIPTS);
    setDocsOpen(false);
    setUserMenuOpen(false);
    setPasswordOpen(false);
  }, [user]);

  // FR-AUT-07's guard. `starting` renders nothing at all rather than a spinner: the session is
  // resolved by one same-origin request, and a splash that appears and vanishes inside 50ms is
  // more disruptive than a briefly empty page already painted in the right theme (R-58(1)).
  if (phase === 'starting') return null;
  if (phase !== 'authenticated' || user === null) {
    return <LoginScreen brandName={brandName} version={GUI_VERSION} />;
  }

  const sidebarUser = {
    initials: initials(user),
    name: displayName(user),
    version: GUI_VERSION,
  };

  return (
    <>
      {/* NFR-A11Y-05. Outside the shell so it survives every surface being swapped, and
          `aria-live="polite"` so it never interrupts. */}
      <div className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </div>
      {/* Outside AppShell so it is an ancestor of BOTH the `chat` slot (where the chips are) and
          the `overlays` slot (where the FR-CIT-03 card renders). See CitationHoverProvider. */}
      <CitationHoverProvider>
        <AppShell
          brandName={brandName}
          showStats={showStats}
          overlays={
            <>
              <CitationCard />
              {/* FR-AUT-09 — an overlay, unlike FR-AUT-08's popover, because it is a modal and
                  is anchored to nothing. */}
              {passwordOpen && (
                <ChangePasswordModal
                  onClose={() => setPasswordOpen(false)}
                  onChanged={() => setAnnouncement(PASSWORD_CHANGED)}
                />
              )}
              {docsOpen && (
                <KnowledgeBaseModal
                  store={documents}
                  conversationId={activeId}
                  onClose={() => setDocsOpen(false)}
                  onCloudImport={() => {
                    // FR-KBM-06 / R-63: T-512 owns the selection surface. Until it lands the
                    // button presents FR-AUT-11's *unlinked* state, which is what the requirement
                    // says it does for an account that has never been linked — and §8.52's rule
                    // is that a prototype primary action is retargeted, never left inert.
                  }}
                />
              )}
            </>
          }
          stats={
            <StatsPanel
              sessionStartedAt={sessionStartedAt}
              entries={chat.entries}
              usage={usage}
              modelName={modelNameOf(chat.entries) ?? SAMPLE_MODEL_NAME}
            />
          }
          chat={
            <>
              <ChatHeader
                title={activeConversation === undefined ? null : displayTitle(activeConversation)}
              />
              <MessageList
                entries={chat.entries}
                typing={chat.typing}
                frozen={chat.frozen}
                conversationId={activeId}
                userInitials={sidebarUser.initials}
                onFeedback={onFeedback}
                onRegenerate={onRegenerate}
              />
              <Composer
                documents={mentionDocuments}
                pending={turnInFlight}
                usage={usage}
                documentCount={knowledgeBaseCount}
                knowledgeBaseOpen={docsOpen}
                onSend={onSend}
                onOpenKnowledgeBase={openKnowledgeBase}
              />
            </>
          }
          sidebar={
            <Sidebar
              brandName={brandName}
              conversations={conversations}
              activeId={activeId}
              onSelect={setActiveId}
              onNewChat={onNewChat}
              onRename={onRename}
              onDelete={onDelete}
              documentCount={knowledgeBaseCount}
              onOpenKnowledgeBase={openKnowledgeBase}
              user={sidebarUser}
              onToggleUserMenu={() => setUserMenuOpen((open) => !open)}
              userMenuOpen={userMenuOpen}
              userMenu={
                userMenuOpen && (
                  <UserMenu
                    email={user.email}
                    onChangePassword={() => {
                      setUserMenuOpen(false);
                      setPasswordOpen(true);
                    }}
                    onSignOut={() => {
                      setUserMenuOpen(false);
                      void signOut();
                    }}
                    onClose={() => setUserMenuOpen(false)}
                  />
                )
              }
            />
          }
        />
      </CitationHoverProvider>
    </>
  );
}

export default App;
