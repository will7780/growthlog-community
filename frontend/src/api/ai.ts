/**
 * AI 相关 API 封装
 */
import { get, post, patch, del } from './client';
import type {
  AgentAnalysis,
  AgentMemoryItem,
  AgentStep,
  AISearchResponse,
  ChatResponse,
  EntryWithChildrenResponse,
  ReferenceItem,
} from '../types/api';

export type { AgentAnalysis, AgentMemoryItem, AgentStep, ReferenceItem };

export type AIMode = 'retrieval' | 'agent' | 'query' | 'hermes' | 'review' | 'organize';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  references?: ReferenceItem[];
  mode?: AIMode;
  agent_steps?: AgentStep[];
  analysis?: AgentAnalysis;
  review_preview?: ReviewPreviewResponse;
  organize_preview?: OrganizePreviewResponse;
  created_at?: string;
}

/** R11.3：会话级固定角色。旧会话（无 persona）在列表中以 legacy=true 只读展示。 */
export type Persona = 'retriever' | 'explainer' | 'organizer';

export const PERSONA_LIST: Persona[] = ['retriever', 'explainer', 'organizer'];

export interface ChatSession {
  session_id: string;
  last_message: string;
  role: string;
  created_at?: string;
  /** null/undefined = 历史会话，没有角色元数据 */
  persona?: Persona | null;
  title?: string | null;
  status?: string | null;
  legacy?: boolean;
  continuable?: boolean;
  source_set_status?: string | null;
  current_source_set_version?: number | null;
  updated_at?: string | null;
}

interface ChatHistoryResponse {
  session_id: string;
  messages: ChatMessage[];
}

interface ChatSessionsResponse {
  sessions: ChatSession[];
  total?: number;
}

interface ChatDeleteResponse {
  success: boolean;
  message: string;
}

export interface CreatePersonaSessionResponse {
  session_id: string;
  persona: Persona;
  title?: string | null;
  status: string;
  legacy: boolean;
  continuable: boolean;
}

/** POST /api/ai/chat/sessions — 创建带固定 persona 的新会话（创建后角色不可变）。 */
export function createPersonaSession(
  persona: Persona,
  title?: string | null
): Promise<CreatePersonaSessionResponse> {
  return post<CreatePersonaSessionResponse>('/api/ai/chat/sessions', {
    persona,
    title: title || undefined,
  });
}

/** 会话列表（含 legacy 与 persona 会话），复用既有 /chat/sessions 分页接口。 */
export function listPersonaSessions(params: { limit?: number; offset?: number } = {}): Promise<ChatSessionsResponse> {
  const query = new URLSearchParams();
  if (params.limit != null) query.set('limit', String(params.limit));
  if (params.offset != null) query.set('offset', String(params.offset));
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return get<ChatSessionsResponse>(`/api/ai/chat/sessions${suffix}`);
}

export interface ChatSessionCompactResponse {
  session_id: string;
  summary: string;
  structured_json: Record<string, unknown>;
  message_count: number;
  last_message_at?: string | null;
}

export interface AgentMemoryCreateRequest {
  memory_type: AgentMemoryItem['memory_type'];
  content: string;
  source?: string;
  confidence?: number;
}

export interface AgentMemoryUpdateRequest {
  memory_type?: AgentMemoryItem['memory_type'];
  content?: string;
  confidence?: number;
  is_active?: boolean;
}

export interface AgentMemoryAnalyzeRequest {
  memory_type: AgentMemoryItem['memory_type'];
  content: string;
  exclude_memory_id?: number | null;
}

export interface AgentMemoryAnalyzeResponse {
  normalized_content: string;
  quality_warnings: Array<Record<string, unknown>>;
}

export interface AgentMemoryMergeRequest {
  source_memory_ids: number[];
  content?: string;
  confidence?: number;
}

export interface AgentMemoryMergeResponse {
  memory: AgentMemoryItem;
  deactivated_memory_ids: number[];
}

export type AgentPendingActionType = 'create_todo' | 'create_memory' | 'update_memory' | 'save_summary_entry';
export type AgentPendingActionStatus = 'pending' | 'confirmed' | 'rejected' | 'executed' | 'failed';

export interface AgentPendingActionItem {
  id: number;
  session_id?: string | null;
  action_type: AgentPendingActionType;
  payload_json: Record<string, unknown>;
  status: AgentPendingActionStatus;
  result_json?: Record<string, unknown> | null;
  error_message?: string | null;
  created_by_message_id?: number | null;
  confirmed_at?: string | null;
  executed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentPendingActionCreateRequest {
  action_type: AgentPendingActionType;
  payload_json: Record<string, unknown>;
  session_id?: string | null;
  created_by_message_id?: number | null;
}

export interface AISearchRequest {
  query: string;
  label_code?: string | null;
  top_k?: number;
  model_key?: string | null;
}

export type ReviewConfirmType = 'review' | 'action_plan' | 'tag_suggestion';

export interface SavedOutputItem {
  id: number;
  type: string;
  source_count?: number;
}

export interface SuggestedOutputItem {
  type: string;
  label: string;
  available?: boolean;
}

export interface ReviewPreviewPayload {
  scope_type?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  tag?: string | null;
  entry_ids?: number[] | null;
  source_entry_ids: number[];
  goal?: string | null;
  message?: string | null;
  title?: string;
  content?: string;
  summary?: string;
  trends: string[];
  keywords: string[];
  suggestions: string[];
  suggested_outputs?: SuggestedOutputItem[];
  saved_outputs?: SavedOutputItem[];
  confirmed?: SavedOutputItem;
  dismissed?: boolean;
  confidence?: number | null;
  metadata?: Record<string, unknown> | null;
  type?: ReviewConfirmType;
  model_key?: string | null;
}

export interface ReviewPreviewResponse extends ReviewPreviewPayload {
  status: string;
  title: string;
  summary: string;
  content?: string;
  episode_id?: string | null;
  facet?: string | null;
  facet_points?: string[];
  entities?: string[];
}

export interface OrganizeSourceDisplay {
  title: string;
  created_at: string;
  kind: string;
  label_code?: string | null;
  ref_token?: string | null;
}

export interface OrganizeDiffItem {
  action: string;
  target_type: string;
  target_id: number | string;
  before: string;
  after: string;
  reason: string;
  risk: string;
  source_entry_ids: number[];
  sources_display?: OrganizeSourceDisplay[];
  confidence?: number | null;
  title?: string | null;
  content?: string | null;
  item_id?: string | null;
  preview_id?: string | null;
}

export type OrganizeDays = 7 | 30 | 90 | null;

export interface OrganizeScopePayload {
  days?: OrganizeDays;
  label_codes?: string[];
  entry_ids?: number[] | null;
  allowed_actions?: string[] | null;
}

export interface OrganizeScopePreviewResponse {
  matched_count: number;
  selected_count: number;
  date_from?: string | null;
  days?: number | null;
  label_codes: string[];
  truncated: boolean;
  max_entries: number;
  source_entry_ids: number[];
}

export interface OrganizePreviewPayload extends OrganizeScopePayload {
  session_id?: string | null;
  message?: string;
  source_entry_ids?: number[] | null;
  goal?: string | null;
  allowed_actions?: string[] | null;
  diff_preview?: OrganizeDiffItem[];
  saved_outputs?: SavedOutputItem[];
  save_summary?: string;
  hermes_protocol?: HermesProtocolInfo | null;
  used_default_entries?: boolean;
  items?: OrganizeDiffItem[];
  metadata?: Record<string, unknown> | null;
  model_key?: string | null;
  preview_id?: string | null;
}

export interface OrganizePreviewResponse {
  status: string;
  message: string;
  goal?: string | null;
  diff_preview: OrganizeDiffItem[];
  source_entry_ids: number[];
  preview_token?: string | null;
  preview_session_id?: string | null;
  used_default_entries?: boolean;
  preview_id?: string | null;
  scope?: {
    matched_count: number;
    selected_count: number;
    date_from?: string | null;
    days?: number | null;
    label_codes?: string[];
    truncated?: boolean;
  } | null;
  hermes_protocol?: HermesProtocolInfo | null;
  saved_outputs?: SavedOutputItem[];
  save_summary?: string;
}

export interface HermesProtocolInfo {
  protocol_version?: string | null;
  request_id?: string | null;
  via?: string | null;
  permission_level?: string | null;
  bridge_execution_source?: string | null;
  runtime_scope?: string | null;
}

export interface AnnotationPreviewResponse {
  status: string;
  entry_id: number;
  source_entry_ids: number[];
  summary: string;
  action_hint?: string;
  confidence?: number | null;
  model_key?: string | null;
}

export type DerivedContentType = 'review' | 'action_plan' | 'tag_suggestion' | 'organize' | 'annotation' | string;

export interface DerivedContentItem {
  id: number;
  type: DerivedContentType;
  title: string;
  content: string;
  source_entry_ids: number[];
  metadata?: Record<string, unknown> | null;
  status?: string;
  created_at: string;
}

export interface DerivedContentDetail extends DerivedContentItem {
  scope_type?: string | null;
  updated_at?: string | null;
}

export interface ScheduledReviewStatusResponse {
  system_enabled?: boolean;
  user_authorized?: boolean;
  effective_enabled?: boolean;
  enabled?: boolean;
  provider?: string;
  review_days?: number;
  poll_interval_seconds?: number;
  scheduled_review_last_at?: string | null;
  pending_count?: number;
  pending_entries?: number;
  draft_count?: number;
  pending_drafts?: number;
  last_run_at?: string | null;
  last_error_code?: string | null;
  status?: string;
  message?: string | null;
}

export const DERIVED_TYPE_LABELS: Record<string, string> = {
  review: '复盘',
  action_plan: '行动计划',
  tag_suggestion: '标签建议',
  organize: '整合',
  annotation: '批注',
};

export const DERIVED_ORIGIN_LABELS: Record<string, string> = {
  manual: '手动',
  hermes: '整合',
  auto_structure: '自动结构化',
  scheduled_review: '定期复盘',
};

export function resolveDerivedOrigin(item?: { metadata?: Record<string, unknown> | null } | Record<string, unknown> | null): string {
  const metadata = item && 'metadata' in item ? item.metadata : item;
  const origin = metadata && typeof metadata === 'object' ? (metadata as Record<string, unknown>).origin : null;
  return typeof origin === 'string' ? origin : 'manual';
}

export function aiSearch(request: AISearchRequest): Promise<AISearchResponse> {
  return post<AISearchResponse>('/api/ai/search', request);
}

export type NormalizedChatResponse = ChatResponse & {
  references: ReferenceItem[];
  analysis?: AgentAnalysis;
  review_preview?: ReviewPreviewResponse;
  organize_preview?: OrganizePreviewResponse;
};

export async function sendChatMessage(
  sessionId: string | null,
  message: string,
  mode: AIMode = 'retrieval',
  extras: OrganizeScopePayload & { goal?: string } = {}
): Promise<NormalizedChatResponse> {
  const endpoint =
    mode === 'agent'
      ? '/api/ai/agent/chat'
      : mode === 'review'
        ? '/api/ai/review/chat'
        : mode === 'organize'
          ? '/api/ai/organize/chat'
          : mode === 'query'
        ? '/api/ai/query/chat'
        : mode === 'hermes'
          ? '/api/ai/hermes/chat'
          : '/api/ai/chat';
  const body: Record<string, unknown> = {
    session_id: sessionId,
    message,
  };
  if (mode === 'organize') {
    body.days = extras.days === undefined ? 30 : extras.days;
    body.label_codes = extras.label_codes || [];
    if (extras.entry_ids && extras.entry_ids.length > 0) {
      body.entry_ids = extras.entry_ids;
    }
    if (extras.allowed_actions && extras.allowed_actions.length > 0) {
      body.allowed_actions = extras.allowed_actions;
    }
    if (extras.goal) body.goal = extras.goal;
  }
  const data = await post<ChatResponse>(endpoint, body);

  return {
    ...data,
    references: data.valid_references || [],
    mode: data.mode || mode,
    agent_steps: data.agent_steps || [],
    analysis: data.analysis || undefined,
    review_preview: (data as NormalizedChatResponse).review_preview,
    organize_preview: (data as NormalizedChatResponse).organize_preview,
  };
}

export function getChatHistory(sessionId: string): Promise<ChatHistoryResponse> {
  return get<ChatHistoryResponse>(`/api/ai/chat/history?session_id=${sessionId}`);
}

export function getChatSessions(): Promise<ChatSessionsResponse> {
  return get<ChatSessionsResponse>('/api/ai/chat/sessions');
}

export function deleteChatSession(sessionId: string): Promise<ChatDeleteResponse> {
  return del<ChatDeleteResponse>(`/api/ai/chat/sessions/${sessionId}`);
}

export function compactChatSession(sessionId: string): Promise<ChatSessionCompactResponse> {
  return post<ChatSessionCompactResponse>(`/api/ai/chat/sessions/${sessionId}/compact`);
}

// ========== R11.3：讲解员来源集（confirm / cancel / check-stale） ==========

export interface SourceSetVersionItem {
  id: number;
  version?: number | null;
  base_version?: number | null;
  status: string;
  content_fingerprint?: string | null;
  member_count: number;
  locked_at?: string | null;
  created_at?: string | null;
}

export interface SourceSetVersionsResponse {
  session_id: string;
  current_version?: number | null;
  versions: SourceSetVersionItem[];
}

export function getSessionSourceSets(sessionId: string): Promise<SourceSetVersionsResponse> {
  return get<SourceSetVersionsResponse>(`/api/ai/chat/sessions/${sessionId}/source-sets`);
}

export interface LockedSourceMember {
  display_index: number;
  ref_token: string;
  source_type: 'entry' | 'attachment' | string;
  title: string;
  snippet: string;
}

export interface CurrentLockedSourcesResponse {
  session_id: string;
  version?: number | null;
  status?: string | null;
  member_count: number;
  stale: boolean;
  can_expand: boolean;
  members: LockedSourceMember[];
  append_only_note: string;
}

/** 讲解员当前固定来源清单（只读）。 */
export function getCurrentLockedSources(sessionId: string): Promise<CurrentLockedSourcesResponse> {
  return get<CurrentLockedSourcesResponse>(`/api/ai/chat/sessions/${sessionId}/source-sets/current`);
}

/** 显式补充来源：主题必填；返回与 pending-proposal 同形的候选。 */
export function expandSourceSet(
  sessionId: string,
  query: string,
): Promise<PendingProposalResponse> {
  return post<PendingProposalResponse>(`/api/ai/chat/sessions/${sessionId}/source-sets/expand`, {
    query,
  });
}

export interface SourceSetConfirmResponse {
  session_id: string;
  version: number;
  content_fingerprint: string;
  status: string;
  member_count: number;
}

/**
 * 确认（讲解员）待处理的来源候选 proposal，生成新的不可变来源版本。
 * 优先使用 selectedRefTokens（opaque）；服务端校验后映射为内部 reference_key。
 */
export function confirmSourceSetProposal(
  sessionId: string,
  payload: {
    proposalId: number;
    baseVersion: number;
    selectedReferenceKeys?: string[];
    selectedRefTokens?: string[];
  },
): Promise<SourceSetConfirmResponse> {
  return post<SourceSetConfirmResponse>(`/api/ai/chat/sessions/${sessionId}/source-sets/confirm`, {
    proposal_id: payload.proposalId,
    base_version: payload.baseVersion,
    selected_reference_keys: payload.selectedReferenceKeys,
    selected_ref_tokens: payload.selectedRefTokens,
  });
}

/** DELETE .../source-sets/proposals/{proposal_id} — 仅能取消 status=proposed；幂等 204。 */
export function cancelSourceSetProposal(sessionId: string, proposalId: number): Promise<void> {
  return del<void>(`/api/ai/chat/sessions/${sessionId}/source-sets/proposals/${proposalId}`);
}

export function checkSourceSetStale(sessionId: string): Promise<{ session_id: string; stale: boolean }> {
  return post<{ session_id: string; stale: boolean }>(`/api/ai/chat/sessions/${sessionId}/source-sets/check-stale`);
}

// ========== R11.3：讲解员非流式 resume（来源确认后继续回答首问/扩展后问题） ==========

export interface ExplainerResumeResponse {
  session_id: string;
  mode: 'explainer';
  phase: string;
  message: string;
  answer?: string | null;
  proposal_id?: number | null;
  valid_references: ReferenceItem[];
  invalid_references: Array<{ entry_id: number; reason: string }>;
  source_set_version?: number | null;
  resumed?: boolean;
  idempotent?: boolean;
}

/** POST /api/ai/explainer/resume — 旧非流式 resume（兼容保留；三角色 UI 不再调用）。 */
export function resumeExplainerProposal(
  sessionId: string,
  proposalId: number
): Promise<ExplainerResumeResponse> {
  return post<ExplainerResumeResponse>('/api/ai/explainer/resume', {
    session_id: sessionId,
    proposal_id: proposalId,
  });
}

/** R11.3-Fix：完整引用预览（ref_token 仅 body）。 */
export interface TodoReferenceNode {
  content: string;
  status: "open" | "completed";
  priority: string | null;
  urgent: boolean;
  due_date?: string | null;
  completed_at?: string | null;
  completion_note?: string | null;
  is_step: boolean;
  sort_order?: number | null;
  depth: number;
  is_hit: boolean;
}

export interface StreamReferencePreviewResponse {
  display_index: number;
  source_type: 'entry' | 'attachment' | 'todo' | 'notion_page';
  reference: ReferenceItem;
  todo_tree?: TodoReferenceNode[] | null;
  open_url?: string | null;
  entry: EntryWithChildrenResponse | null;
  hit_snippet?: string | null;
  filename?: string | null;
  page_no?: number | null;
  slide_no?: number | null;
  can_view_original_image?: boolean;
  can_preview_pdf?: boolean;
}

export function previewStreamReference(
  sessionId: string,
  refToken: string,
): Promise<StreamReferencePreviewResponse> {
  return post<StreamReferencePreviewResponse>(
    `/api/ai/chat/sessions/${sessionId}/references/preview`,
    { ref_token: refToken },
  );
}

export interface PendingProposalResponse {
  pending: boolean;
  proposal_id?: number;
  base_version?: number;
  phase?: string;
  message?: string;
  candidates?: Array<{
    display_index: number;
    ref_token: string;
    source_type: 'entry' | 'attachment' | 'todo' | 'notion_page';
    title: string;
    snippet: string;
  }>;
  pending_user_content?: string;
  expansion_topic?: string | null;
  request_id?: string | null;
}

/** 断线恢复：拉取待确认来源候选。 */
export function getPendingProposal(sessionId: string): Promise<PendingProposalResponse> {
  return get<PendingProposalResponse>(`/api/ai/chat/sessions/${sessionId}/pending-proposal`);
}

export function getAgentMemories(): Promise<AgentMemoryItem[]> {
  return get<AgentMemoryItem[]>('/api/ai/memories');
}

export function createAgentMemory(request: AgentMemoryCreateRequest): Promise<AgentMemoryItem> {
  return post<AgentMemoryItem>('/api/ai/memories', request);
}

export function analyzeAgentMemory(request: AgentMemoryAnalyzeRequest): Promise<AgentMemoryAnalyzeResponse> {
  return post<AgentMemoryAnalyzeResponse>('/api/ai/memories/analyze', request);
}

export function updateAgentMemory(memoryId: number, request: AgentMemoryUpdateRequest): Promise<AgentMemoryItem> {
  return patch<AgentMemoryItem>(`/api/ai/memories/${memoryId}`, request);
}

export function mergeAgentMemories(memoryId: number, request: AgentMemoryMergeRequest): Promise<AgentMemoryMergeResponse> {
  return post<AgentMemoryMergeResponse>(`/api/ai/memories/${memoryId}/merge`, request);
}

export function deleteAgentMemory(memoryId: number): Promise<ChatDeleteResponse> {
  return del<ChatDeleteResponse>(`/api/ai/memories/${memoryId}`);
}

export function getAgentPendingActions(params: { session_id?: string | null; status?: AgentPendingActionStatus } = {}): Promise<AgentPendingActionItem[]> {
  const query = new URLSearchParams();
  if (params.session_id) query.set('session_id', params.session_id);
  if (params.status) query.set('status', params.status);
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return get<AgentPendingActionItem[]>(`/api/ai/agent/pending-actions${suffix}`);
}

export function createAgentPendingAction(request: AgentPendingActionCreateRequest): Promise<AgentPendingActionItem> {
  return post<AgentPendingActionItem>('/api/ai/agent/pending-actions', request);
}

export function confirmAgentPendingAction(actionId: number): Promise<AgentPendingActionItem> {
  return post<AgentPendingActionItem>(`/api/ai/agent/pending-actions/${actionId}/confirm`);
}

export function rejectAgentPendingAction(actionId: number): Promise<AgentPendingActionItem> {
  return post<AgentPendingActionItem>(`/api/ai/agent/pending-actions/${actionId}/reject`);
}

export function extractErrorMessage(error: unknown, fallback = '请求失败'): string {
  if (error instanceof Error) return error.message || fallback;
  if (typeof error === 'string') return error;
  return fallback;
}

export function previewEntryAnnotation(
  entryId: number,
  payload: Record<string, unknown> = {}
): Promise<AnnotationPreviewResponse> {
  return post<AnnotationPreviewResponse>(`/api/ai/entries/${entryId}/annotations/preview`, payload);
}

export function confirmEntryAnnotation(
  entryId: number,
  payload: Record<string, unknown>
): Promise<DerivedContentItem> {
  return post<DerivedContentItem>(`/api/ai/entries/${entryId}/annotations/confirm`, payload);
}

export function previewReview(payload: ReviewPreviewPayload): Promise<ReviewPreviewResponse> {
  return post<ReviewPreviewResponse>('/api/ai/review/preview', payload);
}

export function confirmReview(payload: ReviewPreviewPayload): Promise<DerivedContentItem> {
  return post<DerivedContentItem>('/api/ai/review/confirm', payload);
}

export function previewOrganizeScope(payload: OrganizeScopePayload): Promise<OrganizeScopePreviewResponse> {
  return post<OrganizeScopePreviewResponse>('/api/ai/organize/scope-preview', {
    days: payload.days === undefined ? 30 : payload.days,
    label_codes: payload.label_codes || [],
    entry_ids: payload.entry_ids || null,
  });
}

export function previewOrganize(payload: OrganizePreviewPayload): Promise<OrganizePreviewResponse> {
  return post<OrganizePreviewResponse>('/api/ai/organize/preview', payload);
}

export function confirmOrganize(payload: {
  items: OrganizeDiffItem[];
  goal?: string | null;
  preview_id?: string | null;
  preview_token?: string | null;
  metadata?: Record<string, unknown> | null;
}): Promise<{ created: DerivedContentItem[]; source_entry_ids: number[] }> {
  return post<{ created: DerivedContentItem[]; source_entry_ids: number[] }>('/api/ai/organize/confirm', payload);
}

export function getDerivedContents(params: Record<string, string | number | boolean | null | undefined> | string = {}): Promise<{ items: DerivedContentItem[] }> {
  if (typeof params === 'string') {
    return get<{ items: DerivedContentItem[] }>(`/api/ai/derived-contents?type=${encodeURIComponent(params)}`);
  }
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return get<{ items: DerivedContentItem[] }>(`/api/ai/derived-contents${suffix}`);
}

export function getPendingDraftContents(): Promise<{ items: DerivedContentItem[] }> {
  return getDerivedContents({ status: 'draft' });
}

export function getDerivedContentDetail(contentId: number): Promise<DerivedContentDetail> {
  return get<DerivedContentDetail>(`/api/ai/derived-contents/${contentId}`);
}

export function confirmDerivedContent(contentId: number): Promise<DerivedContentItem> {
  return post<DerivedContentItem>(`/api/ai/derived-contents/${contentId}/confirm`);
}

export function dismissDerivedContent(contentId: number): Promise<DerivedContentItem> {
  return post<DerivedContentItem>(`/api/ai/derived-contents/${contentId}/dismiss`);
}

export function getScheduledReviewStatus(): Promise<ScheduledReviewStatusResponse> {
  return get<ScheduledReviewStatusResponse>('/api/ai/scheduled-review/status');
}

export interface AutoStructureRunResponse {
  status: string;
  processed?: number;
  next_cursor?: string | null;
  message?: string | null;
  [key: string]: unknown;
}

export function runScheduledReview(dryRun?: boolean): Promise<AutoStructureRunResponse> {
  const suffix = dryRun ? '?dry_run=true' : '';
  return post<AutoStructureRunResponse>(`/api/ai/scheduled-review/run${suffix}`);
}

export function runAutoStructure(dryRun?: boolean): Promise<AutoStructureRunResponse> {
  const suffix = dryRun ? '?dry_run=true' : '';
  return post<AutoStructureRunResponse>(`/api/ai/auto-structure/run${suffix}`);
}
