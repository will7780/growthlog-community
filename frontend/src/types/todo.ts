/** 小要事及 Today Plan 类型。 */
export type TodoPriority = 'P0' | 'P1' | 'P2' | 'P3' | 'P4';
export type PlanSource = 'manual' | 'rollover';
export type PlanStatus = 'active' | 'completed' | 'carried' | 'returned';
export type UrgentAction = 'keep' | 'delete';

export interface TodoResponse {
  id: number;
  content: string;
  priority: TodoPriority;
  due_date: string | null;
  is_done: boolean;
  is_urgent: boolean;
  is_expiring_soon: boolean;
  is_overdue: boolean;
  completed_at: string | null;
  completion_note: string | null;
  created_at: string;
  parent_id: number | null;
  sort_order: number | null;
  depth: number;
  family_root_id: number;
  ancestor_titles: string[];
  direct_child_count: number;
  descendant_count: number;
  incomplete_descendant_count: number;
}

export interface TodoTreeNode extends TodoResponse {
  children: TodoTreeNode[];
}

export interface TodoListResponse {
  items: TodoResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface TodoTreeListResponse {
  items: TodoTreeNode[];
  total: number;
  limit: number;
  offset: number;
}

export interface TodoChildrenOrderResponse {
  parent_id: number;
  ordered_child_ids: number[];
}

export interface TodoCreateRequest {
  content: string;
  due_date?: string | null;
  priority?: TodoPriority;
  is_urgent?: boolean;
  add_to_today?: boolean;
}

export interface TodoChildCreateRequest {
  content: string;
  due_date?: string | null;
  priority?: TodoPriority;
  is_urgent?: boolean;
  add_to_today?: boolean;
}

export interface TodoUpdateRequest {
  content?: string;
  due_date?: string | null;
  is_done?: boolean;
  priority?: TodoPriority;
  is_urgent?: boolean;
  completion_note?: string | null;
}

export interface WeeklyStatsResponse {
  week_start: string;
  week_end: string;
  total: number;
  completed: number;
  pending: number;
  completion_rate: number;
}

export interface TodayPlanItemResponse {
  plan_item_id: number;
  plan_date: string;
  source: PlanSource;
  status: PlanStatus;
  todo: TodoResponse;
}

export interface TodayPlanResponse {
  plan_date: string;
  items: TodayPlanItemResponse[];
  completed: number;
  total: number;
  completion_rate: number;
}

export interface UrgentPreviewItem {
  todo: TodoResponse;
  reasons: string[];
  past_active_plan_date?: string | null;
  affected_descendant_count: number;
}

export interface UrgentPreviewResponse {
  plan_date: string;
  items: UrgentPreviewItem[];
}

export interface UrgentConfirmRequest {
  plan_date: string;
  decisions: Array<{
    todo_id: number;
    action: UrgentAction;
    expected_descendant_count?: number;
  }>;
}

export interface UrgentConfirmResponse {
  plan_date: string;
  kept_todo_ids: number[];
  deleted_todo_ids: number[];
}

export interface RolloverPreviewItem {
  todo: TodoResponse;
  past_plan_date: string;
  past_plan_item_id: number;
  affected_descendant_count: number;
}

export interface RolloverPreviewResponse {
  plan_date: string;
  items: RolloverPreviewItem[];
}

export interface RolloverConfirmRequest {
  plan_date: string;
  reviewed_todo_ids: number[];
  selected_todo_ids: number[];
  expected_descendant_counts: Record<number, number>;
}

export interface RolloverConfirmResponse {
  plan_date: string;
  added_todo_ids: number[];
  returned_todo_ids: number[];
}