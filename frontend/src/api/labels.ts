/**
 * 标签相关 API
 */
import { get } from './client';
import type { LabelResponse } from '../types/api';

/**
 * 获取标签列表
 */
export async function getLabels(): Promise<LabelResponse[]> {
  return get<LabelResponse[]>('/api/labels');
}
