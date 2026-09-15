export type NotionUiState =
  | 'unbound'
  | 'authorizing'
  | 'syncing'
  | 'connected'
  | 'partial'
  | 'reauth_required'
  | 'failed'
  | 'disabled';

export interface NotionPreflight {
  enabled: boolean;
  client_configured: boolean;
  secret_configured: boolean;
  redirect_configured: boolean;
  token_key_configured: boolean;
  webhook_token_configured: boolean;
  api_version_configured: boolean;
  sync_enabled: boolean;
}

export interface NotionStatus {
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  ui_state: NotionUiState;
  workspace_name?: string | null;
  sync_status?: string | null;
  connection_status?: string | null;
  page_count?: number | null;
  discovered_count?: number | null;
  indexed_count?: number | null;
  partial_count?: number | null;
  pending_count?: number | null;
  last_sync_completed_at?: string | null;
  last_error_code?: string | null;
  preflight: NotionPreflight;
}

export interface NotionPageListItem {
  title: string;
  breadcrumb: string;
  sync_status: string;
  unsupported_block_count: number;
}

export const NOTION_ERROR_TEXT: Record<string, string> = {
  NOTION_DISABLED: 'Notion 接入未启用。',
  NOTION_NOT_CONFIGURED: 'Notion 接入尚未完成配置。',
  NOTION_OAUTH_STATE_INVALID: '授权状态无效或已被使用。',
  NOTION_OAUTH_STATE_EXPIRED: '授权已过期，请重新连接。',
  NOTION_OAUTH_EXCHANGE_FAILED: 'Notion 授权交换失败，请重试。',
  NOTION_CONNECTION_REQUIRED: '尚未连接 Notion。',
  NOTION_REAUTH_REQUIRED: '需要重新授权 Notion。',
  NOTION_SYNC_IN_PROGRESS: '正在同步 Notion，请稍后再试。',
  NOTION_RATE_LIMITED: 'Notion 请求过于频繁，请稍后重试。',
  NOTION_SYNC_FAILED: 'Notion 同步失败，请稍后重试。',
  NOTION_SOURCE_SYNC_PENDING: '该 Notion 页面正在同步，请稍后再试。',
};
