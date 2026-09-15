import type { TodoPriority, TodoResponse } from '../../types/todo';

export const TODO_PRIORITIES: TodoPriority[] = ['P0', 'P1', 'P2', 'P3', 'P4'];

export const TODO_PRIORITY_LABELS: Record<TodoPriority, string> = {
  P0: '最高',
  P1: '高',
  P2: '中',
  P3: '低',
  P4: '普通',
};

const PRIORITY_RANK: Record<TodoPriority, number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
  P4: 4,
};

export function sortTodos(items: TodoResponse[]): TodoResponse[] {
  return [...items].sort((a, b) => {
    if (a.is_done !== b.is_done) return a.is_done ? 1 : -1;
    if (!a.is_done) {
      const urgentDiff = Number(Boolean(b.is_urgent)) - Number(Boolean(a.is_urgent));
      if (urgentDiff !== 0) return urgentDiff;
      const priorityDiff = PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority];
      if (priorityDiff !== 0) return priorityDiff;
      if (a.due_date !== b.due_date) {
        if (!a.due_date) return 1;
        if (!b.due_date) return -1;
        return a.due_date.localeCompare(b.due_date);
      }
      return b.created_at.localeCompare(a.created_at);
    }
    return (b.completed_at || '').localeCompare(a.completed_at || '');
  });
}
