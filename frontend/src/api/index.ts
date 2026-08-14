/**
 * Readable names for the generated schema types (T-405).
 *
 * Every alias below resolves into `schema.d.ts`, which is generated from the backend's OpenAPI
 * document — so these are *renames of generated types*, not hand-written ones, and the
 * frontend-dev rule ("never hand-write request/response types") is intact. The point is that a
 * component imports `type { Message }` rather than
 * `components['schemas']['MessageResponse']` at twenty call sites.
 *
 * If a name here stops resolving, the backend removed or renamed a model: fix the call sites,
 * never re-declare the shape locally.
 */
import type { components } from './schema';

type Schemas = components['schemas'];

// --- chat (FR-MSG-*, FR-CIT-*) ---
export type Message = Schemas['MessageResponse'];
export type Segment = Schemas['Segment'];
export type CitationSegment = Schemas['CitationSegment'];
export type TextSegment = Schemas['TextSegment'];
export type CitationLocator = Schemas['CitationLocator'];
export type Evaluation = Schemas['EvaluationResponse'];
export type Feedback = Schemas['Feedback'];
/** The served-but-unstored branch of a turn (R-54(3)) — no row, so nothing to rate or
 *  regenerate. Carries the FR-ERR-04 copy in a single text run. */
export type DegradedMessage = Schemas['DegradedMessage'];

// --- the chat stream (R-54(2)) ---
export type ChatFrame = Schemas['ChatStreamFrame'];
export type ChatStageFrame = Schemas['ChatStageFrame'];
export type ChatMessageFrame = Schemas['ChatMessageFrame'];
export type ChatDoneFrame = Schemas['ChatDoneFrame'];
export type TurnStage = Schemas['TurnStage'];
export type TurnOutcome = Schemas['TurnOutcome'];

// --- conversations (FR-SBR-*, FR-ANL-03) ---
export type Conversation = Schemas['ConversationResponse'];
export type ConversationDetail = Schemas['ConversationDetailResponse'];
export type ContextWindow = Schemas['ContextWindowResponse'];

// --- knowledge base (FR-KBM-*) ---
export type Document = Schemas['DocumentResponse'];
export type DocumentEvent = Schemas['DocumentEventResponse'];
export type DocumentFrame = Schemas['DocumentStreamFrame'];
export type DocumentStatus = Schemas['DocumentStatus'];
export type Job = Schemas['JobResponse'];
export type UploadResult = Schemas['UploadResponse'];
export type DeleteResult = Schemas['DeleteResponse'];
export type RetryResult = Schemas['RetryResponse'];
export type ReplaceResult = Schemas['ReplaceResponse'];
/** `global | chat`. Taken off the DTO rather than a schema of its own: FastAPI inlines the
 *  literal on the form and query parameters, so this field is the only named home it has. */
export type UploadScope = Schemas['DocumentResponse']['scope'];

// --- auth (§4.17, FR-AUT-*) ---
export type Token = Schemas['TokenResponse'];
export type Me = Schemas['MeResponse'];

// --- cloud-drive import (FR-KBM-10, FR-AUT-11) ---
/** One row of the FR-KBM-10 selection surface. Metadata only — no URL and nothing that
 *  points at bytes; the `file_id` goes back to the import route and the backend resolves it
 *  against the provider's fixed host (R-63(6)(1)). */
export type DriveFile = Schemas['DriveFileResponse'];
export type DriveListing = Schemas['DriveListResponse'];
export type LinkStatus = Schemas['LinkStatusResponse'];
export type LinkStart = Schemas['LinkStartResponse'];
/** One member today. NFR-CMP-02: v1 commits to Google Drive and the *mechanism* is what is
 *  provider-agnostic, so this widens when a realm gains a second identity provider. */
export type CloudProvider = Schemas['CloudProvider'];

// --- errors ---
/** The ordinary `{detail: string}` body. Render `detail`; never branch on it. */
export type ApiError = Schemas['ErrorResponse'];
/** The `409`s a client must branch on, keyed by `detail.error_code` (R-56(1), R-71(1)). */
export type ContextWindowExceeded = Schemas['ContextWindowExceededResponse'];
export type NotLatestAnswer = Schemas['NotLatestAnswerResponse'];
export type ProcessingLocked = Schemas['ProcessingLockedResponse'];
/** FR-AUT-11's refusal — `ACCOUNT_NOT_LINKED` or `CLOUD_ACCESS_REVOKED`. */
export type CloudLinkRequired = Schemas['CloudLinkRequiredResponse'];
/** The import route's `409` is a union of the two above: the R-24 lock, or a link problem. */
export type ImportConflict = Schemas['ImportConflictResponse'];

// --- config (FR-SYS-03 / FR-ANL-02) ---
export type Config = Schemas['ConfigResponse'];

export {
  api,
  streamFrames,
  StreamError,
  expandPath,
  DOCUMENT_EVENTS_URL,
  CHAT_SEND_PATH,
  CHAT_REGENERATE_PATH,
} from './client';
export { authHeaders, currentAccessToken, setAccessToken } from './auth';
