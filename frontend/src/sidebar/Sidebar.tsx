/**
 * FR-SBR-01..07 — the sidebar's contents.
 *
 * Renders *inside* T-502's `<nav>`; the 264px box, the right border and the `--panel` fill are
 * the shell's, because the prototype puts them on the column element itself and splitting one
 * element's declarations across two stylesheets would need a wrapper the prototype does not
 * have. Everything below is the column's contents, which the prototype pads block by block.
 *
 * Presentational: every mutation is a prop. T-509 owns wiring these to the conversations API
 * ("then wire generated TS client + SSE into all views"), so nothing here fetches.
 */
import styles from './Sidebar.module.css';
import { ConversationList } from './ConversationList';
import type { SidebarConversation } from './conversations';

/** FR-SBR-01's mono badge, and FR-SBR-02's label. */
const RAG_BADGE = 'RAG';
const NEW_CHAT_LABEL = 'New chat';
const KNOWLEDGE_BASE_LABEL = 'Knowledge base';

export interface SidebarUser {
  /** FR-SBR-06 — rendered in the 26px avatar. */
  initials: string;
  name: string;
  /** The mono version tag, e.g. "v1.4". */
  version: string;
}

export interface SidebarProps {
  /** FR-SYS-04, resolved in `App`. FR-SBR-01 renders it beside the accent mark. */
  brandName: string;
  conversations: readonly SidebarConversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  /** FR-SBR-05 — global documents + the active chat's attachments (the same count FR-CMP-06's
   *  composer footer shows, since both name the FR-ORC-06 retrieval scope). */
  documentCount: number;
  /** FR-SBR-05 — opens the §4.7 modal. T-508 supplies the modal itself. */
  onOpenKnowledgeBase: () => void;
  user: SidebarUser;
  /**
   * FR-SBR-06/FR-AUT-08 — toggles the user-menu popover.
   *
   * Optional, and its absence is not a stub: FR-SBR-06 records the row's interactivity as "a
   * §4.17 carve-out addition", and §4.17 — including the popover this opens — is **T-509's**.
   * Until then the row renders exactly as the prototype has it, non-interactive, rather than
   * as a button that looks live and does nothing.
   */
  onToggleUserMenu?: () => void;
  /** Whether that popover is open, for `aria-expanded`. T-509's state. */
  userMenuOpen?: boolean;
}

export function Sidebar({
  brandName,
  conversations,
  activeId,
  onSelect,
  onNewChat,
  onRename,
  onDelete,
  documentCount,
  onOpenKnowledgeBase,
  user,
  onToggleUserMenu,
  userMenuOpen = false,
}: SidebarProps) {
  return (
    <>
      {/* FR-SBR-01. Not a heading: the document's only <h1> is the shell's (T-502), and this
          is the same string, so marking it up as one would put the brand in the heading
          outline twice. */}
      <div className={styles.brand}>
        <span className={styles.brandMark} aria-hidden="true">
          C
        </span>
        <span className={styles.brandName}>{brandName}</span>
        <span className={`${styles.brandBadge} mono`}>{RAG_BADGE}</span>
      </div>

      {/* FR-SBR-02 */}
      <div className={styles.newChatRow}>
        <button type="button" className={styles.newChat} onClick={onNewChat}>
          <span className={styles.newChatPlus} aria-hidden="true">
            +
          </span>
          {NEW_CHAT_LABEL}
        </button>
      </div>

      <ConversationList
        conversations={conversations}
        activeId={activeId}
        onSelect={onSelect}
        onRename={onRename}
        onDelete={onDelete}
      />

      <div className={styles.footer}>
        {/* FR-SBR-05 */}
        <button type="button" className={styles.kbButton} onClick={onOpenKnowledgeBase}>
          <span className={styles.kbBullet} aria-hidden="true" />
          {KNOWLEDGE_BASE_LABEL}
          <span className={`${styles.kbCount} mono`}>{documentCount}</span>
        </button>

        {/* FR-SBR-06 */}
        {onToggleUserMenu === undefined ? (
          <div className={styles.userRow}>
            <UserRowContent user={user} />
          </div>
        ) : (
          <button
            type="button"
            className={`${styles.userRow} ${styles.userRowInteractive}`}
            aria-haspopup="menu"
            aria-expanded={userMenuOpen}
            onClick={onToggleUserMenu}
          >
            <UserRowContent user={user} />
          </button>
        )}
      </div>
    </>
  );
}

function UserRowContent({ user }: { user: SidebarUser }) {
  return (
    <>
      {/* The initials duplicate the name beside them, so they are decorative to a screen
          reader — announcing "M J Maya Jensen" is noise. */}
      <span className={styles.avatar} aria-hidden="true">
        {user.initials}
      </span>
      <span className={styles.userName}>{user.name}</span>
      <span className={`${styles.version} mono`}>{user.version}</span>
    </>
  );
}
