import { del, get, post } from './client';
import type { NotionPageListItem, NotionStatus } from '../types/notion';
import { NOTION_ERROR_TEXT } from '../types/notion';

export function getNotionStatus(): Promise<NotionStatus> {
  return get<NotionStatus>('/api/integrations/notion/status');
}

export function startNotionOAuth(): Promise<{ authorization_url: string }> {
  return post<{ authorization_url: string }>('/api/integrations/notion/oauth/start');
}

export function syncNotionNow(): Promise<{ accepted: boolean; idempotent: boolean }> {
  return post<{ accepted: boolean; idempotent: boolean }>('/api/integrations/notion/sync');
}

export function listNotionPages(cursor?: string): Promise<{ items: NotionPageListItem[]; next_cursor?: string | null }> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return get(`/api/integrations/notion/pages${query}`);
}

export function disconnectNotion(): Promise<{ ok: boolean }> {
  return del<{ ok: boolean }>('/api/integrations/notion/connection');
}

export function notionErrorMessage(err: unknown): string {
  const code = typeof err === 'object' && err && 'code' in err ? String((err as { code?: string }).code || '') : '';
  if (code && NOTION_ERROR_TEXT[code]) return NOTION_ERROR_TEXT[code];
  if (err instanceof Error && err.message) return err.message;
  return 'Notion 操作失败，请稍后重试';
}
