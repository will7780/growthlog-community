/**
 * 记录相关 API
 */
import { get, post, patch, del } from './client';
import type {
  EntryCreateRequest,
  EntryUpdateRequest,
  EntryResponse,
  EntryListResponse,
  EntryWithChildrenResponse
} from '../types/api';

/**
 * 获取记录列表
 */
export async function getEntries(params?: {
  limit?: number;
  offset?: number;
  label_code?: string;
  q?: string;
}): Promise<EntryListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set('limit', params.limit.toString());
  if (params?.offset != null) searchParams.set('offset', params.offset.toString());
  if (params?.label_code) searchParams.set('label_code', params.label_code);
  if (params?.q && params.q.trim()) searchParams.set('q', params.q.trim());

  const query = searchParams.toString();
  return get<EntryListResponse>(`/api/entries${query ? `?${query}` : ''}`);
}

/**
 * 获取单条记录详情
 */
export async function getEntry(entryId: number): Promise<EntryWithChildrenResponse> {
  return get<EntryWithChildrenResponse>(`/api/entries/${entryId}`);
}

/**
 * 创建记录
 */
export async function createEntry(data: EntryCreateRequest): Promise<EntryResponse> {
  return post<EntryResponse>('/api/entries', data);
}

/**
 * 更新记录
 */
export async function updateEntry(entryId: number, data: EntryUpdateRequest): Promise<EntryResponse> {
  return patch<EntryResponse>(`/api/entries/${entryId}`, data);
}

/**
 * 删除记录（含子记录与附件；需 can_edit_delete_own_entries）
 */
export async function deleteEntry(entryId: number): Promise<void> {
  await del<void>(`/api/entries/${entryId}`);
}
