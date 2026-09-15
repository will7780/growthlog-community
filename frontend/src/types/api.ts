/**
 * API 类型定义
 * 与后端真实响应格式严格对齐
 */

// ========== 认证相关 ==========

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string; // "bearer"
  user: UserResponse;
}

export interface UserResponse {
  id: number;
  username: string;
  is_active: boolean;
  is_admin: boolean;
  /** 允许删改自己的原记录；默认 false，与 is_admin 无关 */
  can_edit_delete_own_entries: boolean;
  created_at: string; // ISO 8601 格式
}

export interface AdminUserListResponse {
  items: UserResponse[];
  total: number;
}

export interface AdminCreateUserRequest {
  username: string;
  password: string;
}

export interface AdminUpdateUserRequest {
  is_active?: boolean;
  is_admin?: boolean;
  can_edit_delete_own_entries?: boolean;
}

export interface AdminResetPasswordRequest {
  new_password: string;
}

// ========== 标签相关 ==========

export interface LabelResponse {
  code: string;
  name: string;
  sort_order: number;
}

// ========== 记录相关 ==========

export interface AttachmentResponse {
  id: number;
  entry_id: number;
  user_id: number;
  original_filename: string;
  mime_type: string;
  file_ext: string;
  file_size: number;
  status: 'uploaded' | 'processing' | 'indexed' | 'failed';
  page_count?: number | null;
  slide_count?: number | null;
  error_message?: string | null;
  created_at: string;
  updated_at?: string | null;
  /** Aggregated from chunk metadata for images; do not show provider to users. */
  ocr_status?: 'succeeded' | 'empty' | 'disabled' | 'unavailable' | 'failed' | string | null;
  ocr_provider?: string | null;
  extraction_methods?: string[] | null;
  ocr_reindex_required?: boolean;
}

export interface EntryCreateRequest {
  label_code: string;
  content: string;
  parent_id?: number | null; // 父记录ID，为 null 时创建顶级记录
}

export interface EntryUpdateRequest {
  content?: string;
}

export interface EntryResponse {
  id: number;
  user_id: number;
  label_code: string;
  label_name: string;
  content: string;
  parent_id: number | null;
  created_at: string;
  updated_at: string | null;
  attachments: AttachmentResponse[];
}

export interface EntryWithChildrenResponse extends EntryResponse {
  children: EntryWithChildrenResponse[];
}

export interface EntryListResponse {
  items: EntryWithChildrenResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ========== 错误响应 ==========

export interface ErrorDetail {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface ErrorResponse {
  error: ErrorDetail;
}

// ========== AI 相关 ==========

export interface ReferenceItem {
  entry_id?: number | null;
  source_id?: number | null;
  title: string;
  label_name: string;
  created_at: string | null;
  snippet: string;
  relevance_score: number;
  confidence?: number | null;
  source_reason?: string | null;
  source_type?: 'entry' | 'attachment_chunk' | 'knowledge_source' | 'todo' | 'notion_page';
  attachment_id?: number | null;
  page_no?: number | null;
  slide_no?: number | null;
  modality?: string | null;
  retrieval_method?: string | null;
  retrieval_sources?: string[];
  /** Child hit → parent family (Option B). */
  parent_id?: number | null;
  root_entry_id?: number | null;
  parent_title?: string | null;
  parent_snippet?: string | null;
  metadata?: Record<string, unknown>;
}

export interface InvalidReferenceItem {
  entry_id?: number | null;
  reason: string;
}

export interface AISearchResponse {
  answer: string;
  valid_references: ReferenceItem[];
  invalid_references: InvalidReferenceItem[];
}

export interface ChatResponse {
  session_id: string;
  message: string;
  valid_references: ReferenceItem[];
  invalid_references: InvalidReferenceItem[];
  mode?: 'retrieval' | 'agent' | 'query' | 'hermes' | 'review' | 'organize';
  agent_steps?: AgentStep[];
  analysis?: AgentAnalysis | null;
}

export interface AnalysisEvidenceEntry {
  entry_id?: number;
  title?: string;
  snippet?: string;
  reason?: string;
  label_name?: string;
}

export interface AnalysisEvidenceItem {
  source_type?: string;
  title?: string;
  snippet?: string;
  reason?: string;
}

export interface AnalysisModuleResult {
  evidence_entries?: AnalysisEvidenceEntry[];
  evidence_items?: AnalysisEvidenceItem[];
  [key: string]: unknown;
}

export type AgentAnalysis = {
  timeline?: AnalysisModuleResult;
  weekly_review?: AnalysisModuleResult;
  monthly_review?: AnalysisModuleResult;
  goal_progress?: AnalysisModuleResult;
  emotion_pattern?: AnalysisModuleResult;
  knowledge_clusters?: AnalysisModuleResult;
};

export interface AgentStep {
  tool: string;
  title: string;
  summary: string;
  status?: 'allowed' | 'denied' | 'failed';
  permission?: 'read' | 'write' | 'admin';
  round?: number;
}

export interface AgentMemoryItem {
  id: number;
  memory_type: 'goal' | 'preference' | 'project' | 'profile' | 'insight';
  content: string;
  source: string;
  confidence: number;
  is_active: boolean;
  quality_warnings?: Array<Record<string, unknown>>;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentMemoryMergeResponse {
  memory: AgentMemoryItem;
  deactivated_memory_ids: number[];
}
