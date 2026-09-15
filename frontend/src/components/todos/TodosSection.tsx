/**
 * 小要事：今日计划 + 长期待办树。
 */
import { useEffect } from 'react';
import { ListTodo } from 'lucide-react';
import { useTodos } from '../../contexts/TodoContext';
import TodoForm, { type TodoFormSubmitPayload } from './TodoForm';
import TodoTreeList from './TodoTreeList';
import WeeklyStats from './WeeklyStats';
import ExpiringTodos from './ExpiringTodos';
import MobileNotificationPanel from './MobileNotificationPanel';
import { filterTodoTree } from './todoTree';

export default function TodosSection() {
  const {
    todos,
    todoTree,
    todayPlan,
    todayTodoIds,
    loading,
    error,
    loadTodos,
    loadTodayPlan,
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
  } = useTodos();

  useEffect(() => {
    void loadTodos();
    void loadTodayPlan();
    void loadWeeklyStats();
  }, [loadTodos, loadTodayPlan, loadWeeklyStats]);

  const handleAddTodo = async (payload: TodoFormSubmitPayload) => {
    await addTodo({
      content: payload.content,
      due_date: payload.dueDate,
      priority: payload.priority,
      is_urgent: payload.isUrgent,
      add_to_today: payload.addToToday,
    });
  };

  const todayTree = todoTree.flatMap((root) => (
    todayTodoIds.has(root.id)
      ? [root]
      : filterTodoTree([root], (todo) => todayTodoIds.has(todo.id))
  ));
  const longtermTree = todoTree.flatMap((root) => (
    todayTodoIds.has(root.id)
      ? []
      : filterTodoTree(
          [root],
          (todo) => !todayTodoIds.has(todo.id) && (!todo.is_expiring_soon || todo.is_done),
        )
  ));

  if (loading && todos.length === 0 && !todayPlan) {
    return <div className="todos-section todos-section--loading">加载中...</div>;
  }

  if (error) {
    return <div className="todos-section todos-section--error">{error}</div>;
  }

  const completed = todayPlan?.completed ?? 0;
  const total = todayPlan?.total ?? 0;
  const rate = todayPlan?.completion_rate ?? 0;
  const treeActions = {
    todayTodoIds,
    onToggle: toggleTodo,
    onUpdateDueDate: updateDueDate,
    onUpdatePriority: updatePriority,
    onUpdateUrgent: updateUrgent,
    onDelete: removeTodo,
    onAddChild: addChildTodo,
    onReorderChildren: reorderChildren,
  };

  return (
    <div className="todos-section">
      <h2 className="todos-section__title">
        <ListTodo size={20} aria-hidden="true" />
        小要事
      </h2>

      <section className="today-plan" aria-label="今日计划">
        <div className="today-plan__header">
          <h3 className="today-plan__title">今日计划</h3>
          <div className="today-plan__header-actions">
            <MobileNotificationPanel />
            <div className="today-plan__progress" aria-label={`今日完成 ${completed} / ${total}`}>
              <span>{completed}/{total}</span>
              <span className="today-plan__rate">{rate}%</span>
            </div>
          </div>
        </div>
        <div
          className="today-plan__bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(rate)}
        >
          <span style={{ width: `${Math.min(100, Math.max(0, rate))}%` }} />
        </div>
        <div className="todos-list today-plan__list">
          {todayTree.length ? (
            <TodoTreeList
              {...treeActions}
              nodes={todayTree}
              variant="today"
              onRemoveFromToday={removeFromToday}
            />
          ) : (
            <div className="todos-empty">今天还没有安排，可从下方加入或创建时勾选</div>
          )}
        </div>
      </section>

      <TodoForm onSubmit={handleAddTodo} />

      <ExpiringTodos />

      <WeeklyStats />

      <section className="longterm-todos" aria-label="长期待办">
        <h3 className="longterm-todos__title">长期待办</h3>
        {longtermTree.length ? (
          <div className="todos-list" aria-label="长期待办树">
            <TodoTreeList
              {...treeActions}
              nodes={longtermTree}
              variant="longterm"
              onAddToToday={addToToday}
            />
          </div>
        ) : (
          <div className="todos-empty">暂无其他待办</div>
        )}
      </section>
    </div>
  );
}