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

// --- auth (§4.17, FR-AUT-*) ---
export type Token = Schemas['TokenResponse'];
export type Me = Schemas['MeResponse'];

// --- errors ---
/** The ordinary `{detail: string}` body. Render `detail`; never branch on it. */
export type ApiError = Schemas['ErrorResponse'];
/** The two `409`s a client must branch on, keyed by `detail.error_code` (R-56(1)). */
export type ContextWindowExceeded = Schemas['ContextWindowExceededResponse'];
export type NotLatestAnswer = Schemas['NotLatestAnswerResponse'];

export { api, streamFrames } from './client';
