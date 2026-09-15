import { get, post, put, del } from './client';

export interface UserLabel {
  code: string;
  name: string;
  sort_order: number;
  is_system: boolean;
  can_delete: boolean;
  entry_count: number;
}

export interface UserLabelsResponse {
  labels: UserLabel[];
  total_count: number;
}

export interface CreateLabelRequest {
  name: string;
}

export interface UpdateLabelRequest {
  name?: string;
  sort_order?: number;
}

export interface DeleteLabelRequest {
  target_code?: string;
}

export const profileApi = {
  /** 获取当前用户的标签列表 */
  getUserLabels: async (): Promise<UserLabelsResponse> => {
    return get<UserLabelsResponse>('/api/labels/me');
  },

  /** 创建新标签 */
  createLabel: async (data: CreateLabelRequest): Promise<UserLabel> => {
    return post<UserLabel>('/api/labels', data);
  },

  /** 更新标签 */
  updateLabel: async (code: string, data: UpdateLabelRequest): Promise<UserLabel> => {
    return put<UserLabel>(`/api/labels/${code}`, data);
  },

  /** 删除标签 */
  deleteLabel: async (code: string, targetCode?: string): Promise<void> => {
    return del(`/api/labels/${code}`, { target_code: targetCode });
  },
};