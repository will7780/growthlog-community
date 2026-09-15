/** Small-task context: hierarchy tree + Today's Plan. */
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  addTodoToToday,
  createChildTodo,
  createTodo,
  deleteTodo,
  getTodayPlan,
  getTodoTree,
  getWeeklyStats,
  removeTodoFromToday,
  reorderTodoChildren,
  updateTodo,
} from '../api/todos';
import type {
  TodayPlanResponse,
  TodoChildCreateRequest,
  TodoCreateRequest,
  TodoPriority,
  TodoResponse,
  TodoTreeNode,
  WeeklyStatsResponse,
} from '../types/todo';
import { flattenTodoTree } from '../components/todos/todoTree';

interface TodoContextType {
  todos: TodoResponse[];
  todoTree: TodoTreeNode[];
  todayPlan: TodayPlanResponse | null;
  todayTodoIds: Set<number>;
  loading: boolean;
  error: string | null;
  weeklyStats: WeeklyStatsResponse | null;
  loadTodos: () => Promise<void>;
  loadTodayPlan: () => Promise<void>;
  refreshAll: () => Promise<void>;
  addTodo: (data: TodoCreateRequest) => Promise<TodoResponse>;
  addChildTodo: (parentId: number, data: TodoChildCreateRequest) => Promise<TodoResponse>;
  toggleTodo: (id: number, isDone: boolean, completionNote?: string | null) => Promise<void>;
  updateDueDate: (id: number, dueDate: string | null) => Promise<void>;
  updatePriority: (id: number, priority: TodoPriority) => Promise<void>;
  updateUrgent: (id: number, isUrgent: boolean) => Promise<void>;
  removeTodo: (id: number, expectedDescendantCount: number) => Promise<void>;
  reorderChildren: (parentId: number, orderedChildIds: number[]) => Promise<void>;
  addToToday: (id: number) => Promise<void>;
  removeFromToday: (id: number) => Promise<void>;
  loadWeeklyStats: () => Promise<void>;
}

const TodoContext = createContext<TodoContextType | undefined>(undefined);

export function TodoProvider({ children }: { children: ReactNode }) {
  const [todos, setTodos] = useState<TodoResponse[]>([]);
  const [todoTree, setTodoTree] = useState<TodoTreeNode[]>([]);
  const [todayPlan, setTodayPlan] = useState<TodayPlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [weeklyStats, setWeeklyStats] = useState<WeeklyStatsResponse | null>(null);

  const todayTodoIds = useMemo(
    () => new Set((todayPlan?.items || []).map((item) => item.todo.id)),
    [todayPlan],
  );

  const loadTodos = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getTodoTree({ limit: 100 });
      setTodoTree(response.items);
      setTodos(flattenTodoTree(response.items));
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTodayPlan = useCallback(async () => {
    try {
      setTodayPlan(await getTodayPlan());
    } catch {
      /* Today Plan failure does not hide the long-term list. */
    }
  }, []);

  const loadWeeklyStats = useCallback(async () => {
    try {
      setWeeklyStats(await getWeeklyStats());
    } catch {
      /* Non-critical aggregate. */
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([loadTodos(), loadTodayPlan(), loadWeeklyStats()]);
  }, [loadTodos, loadTodayPlan, loadWeeklyStats]);

  const addTodo = useCallback(async (data: TodoCreateRequest) => {
    const created = await createTodo(data);
    await refreshAll();
    return created;
  }, [refreshAll]);

  const addChildTodo = useCallback(async (parentId: number, data: TodoChildCreateRequest) => {
    const created = await createChildTodo(parentId, data);
    await refreshAll();
    return created;
  }, [refreshAll]);

  const toggleTodo = useCallback(async (
    id: number,
    isDone: boolean,
    completionNote?: string | null,
  ) => {
    const payload: Parameters<typeof updateTodo>[1] = { is_done: isDone };
    if (isDone) payload.completion_note = completionNote?.trim() || null;
    await updateTodo(id, payload);
    await refreshAll();
  }, [refreshAll]);

  const updateDueDate = useCallback(async (id: number, dueDate: string | null) => {
    await updateTodo(id, { due_date: dueDate });
    await loadTodos();
  }, [loadTodos]);

  const updatePriority = useCallback(async (id: number, priority: TodoPriority) => {
    await updateTodo(id, { priority });
    await loadTodos();
  }, [loadTodos]);

  const updateUrgent = useCallback(async (id: number, isUrgent: boolean) => {
    await updateTodo(id, { is_urgent: isUrgent });
    await loadTodos();
  }, [loadTodos]);

  const removeTodo = useCallback(async (id: number, expectedDescendantCount: number) => {
    await deleteTodo(id, expectedDescendantCount);
    await refreshAll();
  }, [refreshAll]);

  const reorderChildren = useCallback(async (parentId: number, orderedChildIds: number[]) => {
    const previous = todoTree;
    const order = new Map(orderedChildIds.map((id, index) => [id, index]));
    const reorder = (nodes: TodoTreeNode[]): TodoTreeNode[] => nodes.map((node) => {
      const children = node.id === parentId
        ? [...node.children]
            .sort((left, right) => (order.get(left.id) ?? Number.MAX_SAFE_INTEGER) - (order.get(right.id) ?? Number.MAX_SAFE_INTEGER))
            .map((child, index) => ({ ...child, sort_order: index }))
        : reorder(node.children);
      return { ...node, children };
    });
    const optimistic = reorder(previous);
    setTodoTree(optimistic);
    setTodos(flattenTodoTree(optimistic));
    try {
      await reorderTodoChildren(parentId, orderedChildIds);
    } catch (error) {
      setTodoTree(previous);
      setTodos(flattenTodoTree(previous));
      const code = (error as Error & { code?: string }).code;
      if (code === 'TODO_ORDER_STALE') await loadTodos();
      throw error;
    }
  }, [loadTodos, todoTree]);
  const addToToday = useCallback(async (id: number) => {
    await addTodoToToday(id);
    await refreshAll();
  }, [refreshAll]);

  const removeFromToday = useCallback(async (id: number) => {
    await removeTodoFromToday(id);
    await refreshAll();
  }, [refreshAll]);

  return (
    <TodoContext.Provider value={{
      todos,
      todoTree,
      todayPlan,
      todayTodoIds,
      loading,
      error,
      weeklyStats,
      loadTodos,
      loadTodayPlan,
      refreshAll,
      addTodo,
      addChildTodo,
      toggleTodo,
      updateDueDate,
      updatePriority,
      updateUrgent,
      removeTodo,
      reorderChildren,
      addToToday,
      removeFromToday,
      loadWeeklyStats,
    }}>
      {children}
    </TodoContext.Provider>
  );
}

export function useTodos() {
  const context = useContext(TodoContext);
  if (!context) throw new Error('useTodos must be used within a TodoProvider');
  return context;
}