/**
 * Corpus GUI root. Owns the FR-SYS-04 configurable-prop boundary, mounts the theme runtime
 * (§4.8) and composes the FR-LAY-01 shell inside it (R-58(5)).
 *
 * This is the composition root: it is where the FR-SYS-04 defaults resolve, where T-503..T-508
 * fill the shell's slots, and where T-509 branches between the §4.17 login screen and the
 * shell.
 *
 * **The FR-CST-01 state below is local and seeded.** T-509 owns wiring the generated client
 * into every view; until then the sidebar is driven from `useState` so its behaviour is
 * buildable and testable. Every mutation here is the shape of the API call that replaces it —
 * `onRename` is `PATCH /conversations/{id}`, `onDelete` is `DELETE /conversations/{id}`.
 */
import { useCallback, useRef, useState } from 'react';
import { ThemeProvider } from './theme/ThemeProvider';
import { AppShell } from './shell/AppShell';
import { Sidebar } from './sidebar/Sidebar';
import { ChatHeader } from './chat/ChatHeader';
import { displayTitle, nextActiveId, UNTITLED_CONVERSATION } from './sidebar/conversations';
import type { SidebarConversation } from './sidebar/conversations';

/**
 * FR-SYS-04 / §9. The only place this literal exists on the JS side.
 *
 * Resolved here rather than in `AppShell` because `brandName` has consumers on both sides of
 * the shell boundary — FR-SBR-01's brand row inside it, and T-509's FR-AUT-01 login card,
 * which replaces the shell entirely rather than living within it.
 */
const DEFAULT_BRAND_NAME = 'Corpus';

/** FR-SBR-06's sample identity — the prototype's, and replaced by `GET /api/v1/auth/me` in
 *  T-509. The version tag is the §9/Appendix A "v1.4". */
const SAMPLE_USER = { initials: 'MJ', name: 'Maya Jensen', version: 'v1.4' };

/** Sample conversations, standing in for `GET /api/v1/conversations` until T-509. Shaped as
 *  the generated `Conversation` model so the swap is a data-source change, not a type change. */
const SAMPLE_CONVERSATIONS: SidebarConversation[] = [
  seed('Analyzing Market Trends', '2026-07-16T09:12:00Z', 2),
  seed('Product Launch Strategy', '2026-07-14T16:40:00Z', 2),
  seed('Customer Persona Refinement', '2026-07-11T11:05:00Z', 0),
  seed('Pricing Experiment Review', '2026-07-08T08:30:00Z', 0),
];

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

function App({ accent, brandName = DEFAULT_BRAND_NAME, showStats = true }: AppProps) {
  const [conversations, setConversations] = useState<SidebarConversation[]>(SAMPLE_CONVERSATIONS);
  const [activeId, setActiveId] = useState<string | null>(SAMPLE_CONVERSATIONS[0]?.id ?? null);
  const nextId = useRef(0);

  // FR-SBR-02 — prepends an empty chat and activates it, showing the empty state.
  const onNewChat = useCallback(() => {
    const created = seed(UNTITLED_CONVERSATION, new Date().toISOString(), 0);
    // `seed` derives its id from the title, so every "New chat" would collide. A counter
    // rather than `crypto.randomUUID()`: that is undefined outside a secure context, so it
    // would throw on an HTTP LAN deployment. The real id comes from
    // `POST /api/v1/conversations` in T-509.
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

  return (
    <ThemeProvider accent={accent}>
      <AppShell
        brandName={brandName}
        showStats={showStats}
        chat={
          <ChatHeader
            title={activeConversation === undefined ? null : displayTitle(activeConversation)}
          />
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
            documentCount={5}
            onOpenKnowledgeBase={() => {
              // T-508 owns the §4.7 modal.
            }}
            user={SAMPLE_USER}
          />
        }
      />
    </ThemeProvider>
  );
}

export default App;
