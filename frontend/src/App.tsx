/**
 * Corpus GUI root. Owns the FR-SYS-04 configurable-prop boundary, mounts the theme runtime
 * (§4.8) and composes the FR-LAY-01 shell inside it (R-58(5)).
 *
 * This is the composition root: it is where the FR-SYS-04 defaults resolve, where T-503..T-508
 * fill the shell's slots, and where T-509 branches between the §4.17 login screen and the shell.
 *
 * **Every surface now runs on the server (T-513).** The seeded conversations, the seeded
 * transcripts, the local `nextId` counter and the two hard-coded usage figures are gone; four
 * stores stand in their place — `useConversations` (the FR-SBR-03 list), `useChat` (the §4.3
 * transcript, the turn and the FR-ANL-03 meter), `useDocuments` (the FR-KBM-* set, T-508's) and
 * `useConfig` (FR-SYS-03's model id). What is left here is composition: `activeId`, the session
 * UI flags, and the two arrows the stores cannot draw between themselves.
 *
 * **The one binding worth naming is `turnInFlight`** — OI-31/R-71(1)'s "a response is
 * generating", read from the chat store and handed to `useDocuments` so the KB modal's four
 * verbs, FR-MSG-05's dots and FR-MSG-08's action bar are the same fact rather than three. A
 * source guard in `App.test.tsx` pins it.
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
import { AppShell, MAIN_ID } from './shell/AppShell';
import { Sidebar } from './sidebar/Sidebar';
import { ChatHeader } from './chat/ChatHeader';
import { CitationCard } from './chat/CitationCard';
import { CitationHoverProvider } from './chat/CitationHoverProvider';
import { MessageList } from './chat/MessageList';
import { useChat, toProjection } from './chat/useChat';
import { Composer } from './composer/Composer';
import { mentionedIds } from './composer/mentions';
import { readLinkReturn } from './cloud/cloud';
import { useCloudFiles } from './cloud/useCloudFiles';
import { useCloudLink } from './cloud/useCloudLink';
import { KnowledgeBaseModal } from './kb/KnowledgeBaseModal';
import { documentCount, toMentionDocuments } from './kb/documents';
import { useDocuments } from './kb/useDocuments';
import { StatsPanel } from './stats/StatsPanel';
import { modelNameOf } from './stats/stats';
import { useConfig } from './stats/useConfig';
import { displayTitle, nextActiveId } from './sidebar/conversations';
import { useConversations } from './sidebar/useConversations';

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

/**
 * FR-AUT-11's three return outcomes, announced on the NFR-A11Y-05 live region. TBD(§8.4).
 *
 * They are announced rather than rendered as a notice because the surfaces themselves already
 * carry the visible answer: on success the picker opens listing files, and on either failure the
 * FR-KBM-06 button is still offering to link. What the live region adds is the half a sighted
 * user gets from that change of state and a screen-reader user otherwise would not — the page
 * has just reloaded, so there is no transition for it to infer.
 *
 * `denied` is the user's own cancellation at the consent screen and says so; `failed` is
 * everything else, and deliberately does not speculate about the cause, which the GUI never
 * learns (the vocabulary is three words wide).
 */
const LINK_RETURN_COPY = {
  linked: 'Google Drive connected.',
  failed: 'Connecting Google Drive did not complete.',
  denied: 'Connecting Google Drive was cancelled.',
} as const;

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
  /** The one fact both stores read and neither owns. */
  const [activeId, setActiveId] = useState<string | null>(null);
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
  /** FR-KBM-10's picker. Here rather than inside the modal because the `?link=` return has to
   *  open it on a cold page load, before the modal exists. */
  const [cloudOpen, setCloudOpen] = useState(false);

  /**
   * FR-AUT-11's return leg, read ONCE at mount.
   *
   * Keycloak sends the browser to `CLOUD_RETURN_URL?link=linked|failed|denied` — a full page
   * load, not a route change — so this is the only signal that the user has come back from
   * linking. Read in a `useState` initializer rather than an effect because StrictMode runs
   * effects twice (§8.59) and the strip below would make the second pass see nothing; taking it
   * at first render means the value is captured before anything can remove it.
   */
  const [linkReturn, setLinkReturn] = useState(() => readLinkReturn(window.location.search));

  // `enabled` rather than a conditional call, on all four stores: hooks cannot be conditional,
  // so these run during the `starting` phase too — and an unauthenticated call there answers
  // 401, which FR-AUT-07's handler would read as "the session ended", signing out the very
  // user whose session was in the middle of resuming (§8.59).
  const authenticated = phase === 'authenticated';

  const conversations = useConversations({ enabled: authenticated });

  /**
   * The two arrows the stores cannot draw between themselves.
   *
   * A finished turn moves the sidebar row (`updated_at`, the message count) and a 404 removes
   * it — both are facts the chat store learns and the list store owns. Passing them through
   * here is one hop and saves a third GET; a shared context would be the alternative, and
   * `useDocuments`' docstring already argues against introducing one.
   */
  const onTurnSettled = useCallback(
    (id: string, updated: { updatedAt: string; messageCount: number }) => {
      conversations.patch(id, updated);
    },
    [conversations],
  );
  const onMissing = useCallback(
    (id: string) => {
      conversations.forget(id);
      setActiveId((current) => (current === id ? nextActiveId(conversations.rows, id) : current));
    },
    [conversations],
  );

  const chat = useChat({
    conversationId: activeId,
    enabled: authenticated,
    onTurnSettled,
    onMissing,
  });

  /**
   * OI-31 / OI-36 (R-71(1)) — the client-side "a turn is in flight" signal, in ONE place.
   *
   * FR-MSG-05's typing indicator, FR-MSG-08's action bar and FR-KBM-07's four gated verbs are
   * the same fact rendered three times, and R-43(4)'s server `409` is a fourth copy that can
   * disagree with all of them. It is per **user**, not per conversation, because that is what
   * the R-24 lock is keyed on — the server refuses a document verb while a turn runs in a
   * *different* chat, so a per-chat signal here would disagree with the `409` it pre-empts.
   */
  const turnInFlight = chat.turnInFlight;

  /**
   * The FR-KBM-* store. It owns `GET /documents`, the R-41 event stream and the four verbs —
   * T-508 wires its own surface, which R-69(5) records as this project's convention.
   */
  const documents = useDocuments({
    open: docsOpen,
    conversationId: activeId,
    turnInFlight,
    enabled: authenticated,
  });
  const mentionDocuments = toMentionDocuments(documents.rows, activeId);
  const knowledgeBaseCount = documentCount(documents.rows, activeId);

  const config = useConfig({ enabled: authenticated });

  /** FR-AUT-11's linked state, and FR-KBM-10's file list. The link status is read whenever there
   *  is a session; the list only while the picker is open, because it spends a third party's
   *  quota against a rate limit shared with import. */
  const cloudLink = useCloudLink({ enabled: authenticated });
  const cloudFiles = useCloudFiles({ active: cloudOpen && authenticated });

  const openKnowledgeBase = useCallback(() => setDocsOpen(true), []);

  /**
   * FR-KBM-06's two behaviours: "the button keeps its prototype appearance and position; when
   * the user has not yet linked an account it initiates linking rather than opening the
   * selection surface."
   *
   * Waiting for `loaded` is not defensiveness. Guessing "not linked" before the status arrives
   * would navigate an already-linked user away from the app for no reason, and a full-page
   * redirect is the one action on this surface the Back button does not cleanly undo.
   */
  const onCloudImport = useCallback(() => {
    if (!cloudLink.loaded) return;
    if (cloudLink.linked) setCloudOpen(true);
    else void cloudLink.beginLink();
  }, [cloudLink]);

  /**
   * The return from linking, acted on once the session has resumed.
   *
   * It cannot be acted on any earlier: this is a cold load, so `phase` is `starting` for the
   * first round trip and the surfaces below do not exist yet. On success both the modal and the
   * picker open — the user pressed a button, was sent to Google and came back, and landing them
   * on a plain app would lose that thread. On `failed`/`denied` the modal opens alone, with the
   * FR-KBM-06 button still offering to link.
   *
   * `link.refresh()` on success is what turns the button's branch over: the status was read
   * before the redirect and still says "not linked".
   */
  // Destructured, not read off `cloudLink` inside the effect: the store is a fresh object every
  // render, so depending on it would re-run this until `linkReturn` cleared. `refresh` is a
  // stable `useCallback`, which makes the dependency list honest rather than suppressed.
  const refreshLink = cloudLink.refresh;
  useEffect(() => {
    if (linkReturn === null || !authenticated) return;
    setDocsOpen(true);
    if (linkReturn === 'linked') {
      refreshLink();
      setCloudOpen(true);
    }
    setAnnouncement(LINK_RETURN_COPY[linkReturn]);
    setLinkReturn(null);
  }, [authenticated, linkReturn, refreshLink]);

  /** Strip the query string so a refresh cannot re-fire the return. Separate from the effect
   *  above because it must happen whether or not a session ever resumes — an unauthenticated
   *  user landing here should not keep `?link=` in the address bar through the login screen. */
  useEffect(() => {
    if (readLinkReturn(window.location.search) === null) return;
    window.history.replaceState(null, '', window.location.pathname);
  }, []);

  // FR-SBR-02 — `POST /conversations`, whose 201 already carries the FR-ANL-03 meter, so the
  // new chat needs no follow-up GET before the composer can project FR-STA-04 against it.
  const onNewChat = useCallback(async () => {
    const created = await conversations.create();
    if (created === null) return null;
    chat.adopt(created.id, created.context);
    setActiveId(created.id);
    return created.id;
  }, [chat, conversations]);

  /**
   * FR-CMP-03's send.
   *
   * Creating the conversation first is the one path that spans both stores, and it is
   * reachable rather than defensive: FR-SBR-07 leaves `activeId` null once the last chat is
   * deleted, and the empty state still has a composer.
   *
   * **The created id travels as an argument (B-001).** `onNewChat` calls `setActiveId`, but this
   * callback was built in a render where `activeId` was `null` and it keeps that value for its
   * whole life — so handing the send nothing means handing it `null`, which `chat.send` refuses.
   * The turn then vanishes: no request, no bubble, and nothing in the server log to explain it.
   */
  const onSend = useCallback(
    (text: string) => {
      const documentIds = mentionedIds(text, mentionDocuments);
      if (activeId !== null) {
        chat.send(text, documentIds);
        return;
      }
      void onNewChat().then((id) => {
        if (id !== null) chat.send(text, documentIds, id);
      });
    },
    [activeId, chat, mentionDocuments, onNewChat],
  );

  // FR-SBR-07 — "if it was active, selects the next conversation or the empty state".
  // Two separate updaters, deliberately: calling `setActiveId` inside a `setState` updater
  // would be a side effect in a function StrictMode invokes twice.
  const onDelete = useCallback(
    async (id: string) => {
      const failure = await chat.remove(id);
      if (failure !== null) return failure;
      setActiveId((current) => (current === id ? nextActiveId(conversations.rows, id) : current));
      conversations.forget(id);
      return null;
    },
    [chat, conversations],
  );

  // FR-HDR-01. `undefined` once the last conversation is deleted (FR-SBR-07 leaves `activeId`
  // null), which `ChatHeader` renders as the untitled-chat label — see `ChatHeaderProps.title`.
  const activeConversation = conversations.rows.find((row) => row.id === activeId);

  // FR-SBR-04 — the first chat is selected once the list arrives, so the app opens on a
  // transcript rather than on an empty state the user did not ask for.
  useEffect(() => {
    if (!conversations.loaded || activeId !== null) return;
    setActiveId(conversations.rows[0]?.id ?? null);
  }, [activeId, conversations.loaded, conversations.rows]);

  // ONE expression, two consumers: the FR-ANL-03 meter and the composer's FR-STA-04 projection
  // read the same numbers, so the panel cannot say a chat has room while the composer refuses
  // it. `null` for the one round trip after activation — see `StatsPanelProps.usage`.
  const usage = chat.usage === null ? null : toProjection(chat.usage);

  /**
   * FR-AUT-06's (D), resolved by R-72(3): the conversation the user was in **is** restored
   * after re-login — but only for the same user.
   *
   * Restoring is the default rather than the work, because this component stays mounted across
   * the session drop (only what it *returns* changes). The work is the reset, and it is the half
   * that matters: without it, a colleague signing in on the expiry screen would land in the
   * previous user's chat pointer — which `404`s under R-54(1), but should never be attempted.
   * The stores re-read themselves off `enabled`, so clearing the pointer is all this has to do.
   */
  const lastUserId = useRef<string | null>(null);
  useEffect(() => {
    if (user === null) return;
    const previous = lastUserId.current;
    lastUserId.current = user.id;
    if (previous === null || previous === user.id) return;
    setActiveId(null);
    setDocsOpen(false);
    // The picker belongs to the previous user's Drive account, and `useCloudLink` has already
    // forgotten the link it was showing. Leaving it open would list the new user's files under
    // the old one's address for a render.
    setCloudOpen(false);
    setUserMenuOpen(false);
    setPasswordOpen(false);
  }, [user]);

  /**
   * NFR-A11Y-04 (T-511). Signing in replaces the entire tree — the login screen and the shell
   * are different components, not two states of one — so the element that had focus is
   * detached and focus falls back to `<body>`. Measured live: `document.activeElement` was
   * `BODY` immediately after a successful sign-in, which leaves a keyboard user at the very
   * top of the document with no indication anything happened, and a screen-reader user with
   * no cursor in the new page at all.
   *
   * `<main>` is the target because the skip link already uses it (same `MAIN_ID`, same
   * `tabIndex={-1}`), so this introduces no new focusable element and no new pixel: the ring
   * is `:focus-visible`-only and programmatic focus after a form submit does not paint it —
   * verified in both themes for T-510.
   *
   * Guarded on the *transition* rather than on `phase`, so a re-render while already
   * authenticated never yanks focus out from under the user mid-task.
   */
  const lastPhase = useRef(phase);
  useEffect(() => {
    const previous = lastPhase.current;
    lastPhase.current = phase;
    if (phase !== 'authenticated' || previous === 'authenticated') return;
    document.getElementById(MAIN_ID)?.focus();
  }, [phase]);

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
                  onClose={() => {
                    // The picker goes with it: it is nested inside this modal, so leaving the
                    // flag set would reopen it the next time the modal is opened, on a surface
                    // the user reached for a different reason.
                    setCloudOpen(false);
                    setDocsOpen(false);
                  }}
                  onCloudImport={onCloudImport}
                  cloud={{
                    open: cloudOpen,
                    files: cloudFiles,
                    link: cloudLink,
                    onClose: () => setCloudOpen(false),
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
              // The answering model wins where there is one — an answered chat should name the
              // model that produced it, not the one configured since. FR-SYS-03's configured id
              // is the fallback, and it is the only thing an unanswered chat can truthfully show.
              modelName={modelNameOf(chat.entries) ?? config?.chat_model ?? ''}
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
                onFeedback={chat.feedback}
                onRegenerate={chat.regenerate}
                onAnswerUngrounded={chat.answerUngrounded}
                ungroundedBusy={chat.ungroundedBusy}
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
              conversations={conversations.rows}
              activeId={activeId}
              onSelect={setActiveId}
              onNewChat={() => void onNewChat()}
              onRename={conversations.rename}
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
