/** 小要事 API。 */
import { del, get, patch, post, put } from './client';
import type {
  RolloverConfirmRequest,
  RolloverConfirmResponse,
  RolloverPreviewResponse,
  TodayPlanItemResponse,
  TodayPlanResponse,
  TodoChildCreateRequest,
  TodoChildrenOrderResponse,
  TodoCreateRequest,
  TodoListResponse,
  TodoResponse,
  TodoTreeListResponse,
  TodoUpdateRequest,
  UrgentConfirmRequest,
  UrgentConfirmResponse,
  UrgentPreviewResponse,
  WeeklyStatsResponse,
} from '../types/todo';

export async function getTodos(params?: {
  status?: 'pending' | 'done';
  limit?: number;
  offset?: number;
}): Promise<TodoListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.status) searchParams.set('status', params.status);
  if (params?.limit) searchParams.set('limit', params.limit.toString());
  if (params?.offset) searchParams.set('offset', params.offset.toString());
  const query = searchParams.toString();
  return get<TodoListResponse>(`/api/todos${query ? `?${query}` : ''}`);
}

export async function getTodoTree(params?: {
  limit?: number;
  offset?: number;
}): Promise<TodoTreeListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.limit) searchParams.set('limit', params.limit.toString());
  if (params?.offset) searchParams.set('offset', params.offset.toString());
  const query = searchParams.toString();
  return get<TodoTreeListResponse>(`/api/todos/tree${query ? `?${query}` : ''}`);
}

export async function createTodo(data: TodoCreateRequest): Promise<TodoResponse> {
  return post<TodoResponse>('/api/todos', data);
}

export async function createChildTodo(
  parentId: number,
  data: TodoChildCreateRequest,
): Promise<TodoResponse> {
  return post<TodoResponse>(`/api/todos/${parentId}/children`, data);
}

export async function updateTodo(id: number, data: TodoUpdateRequest): Promise<TodoResponse> {
  return patch<TodoResponse>(`/api/todos/${id}`, data);
}

export async function reorderTodoChildren(
  parentId: number,
  orderedChildIds: number[],
): Promise<TodoChildrenOrderResponse> {
  return put<TodoChildrenOrderResponse>('/api/todos/' + parentId + '/children/order', {
    ordered_child_ids: orderedChildIds,
  });
}

export async function deleteTodo(id: number, expectedDescendantCount = 0): Promise<void> {
  const query = new URLSearchParams({
    expected_descendant_count: String(expectedDescendantCount),
  });
  return del<void>(`/api/todos/${id}?${query.toString()}`);
}

export async function getWeeklyStats(): Promise<WeeklyStatsResponse> {
  return get<WeeklyStatsResponse>('/api/todos/stats/weekly');
}

export async function getTodayPlan(): Promise<TodayPlanResponse> {
  return get<TodayPlanResponse>('/api/todos/today-plan');
}

export async function addTodoToToday(todoId: number): Promise<TodayPlanItemResponse> {
  return post<TodayPlanItemResponse>(`/api/todos/${todoId}/today-plan`);
}

export async function removeTodoFromToday(todoId: number): Promise<void> {
  return del<void>(`/api/todos/${todoId}/today-plan`);
}

export async function getUrgentPreview(): Promise<UrgentPreviewResponse> {
  return get<UrgentPreviewResponse>('/api/todos/today-plan/urgent-preview');
}

export async function confirmUrgentReview(
  data: UrgentConfirmRequest,
): Promise<UrgentConfirmResponse> {
  return post<UrgentConfirmResponse>('/api/todos/today-plan/urgent-confirm', data);
}

export async function getRolloverPreview(): Promise<RolloverPreviewResponse> {
  return get<RolloverPreviewResponse>('/api/todos/today-plan/rollover-preview');
}

export async function confirmRollover(
  data: RolloverConfirmRequest,
): Promise<RolloverConfirmResponse> {
  return post<RolloverConfirmResponse>('/api/todos/today-plan/rollover-confirm', data);
}