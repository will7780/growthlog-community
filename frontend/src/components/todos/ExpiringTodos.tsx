/**
 * 即将到期（排除已在今日计划中的任务，避免重复）
 */
import { Clock3 } from 'lucide-react';
import { useTodos } from '../../contexts/TodoContext';
import TodoItem from './TodoItem';

export default function ExpiringTodos() {
  const {
    todos,
    todayTodoIds,
    toggleTodo,
    updateDueDate,
    updatePriority,
    updateUrgent,
    removeTodo,
    addToToday,
  } = useTodos();

  const expiringTodos = todos.filter(
    (todo) => todo.is_expiring_soon && !todo.is_done && !todayTodoIds.has(todo.id),
  );

  if (expiringTodos.length === 0) {
    return null;
  }

  return (
    <div className="expiring-todos">
      <div className="expiring-todos__header">
        <Clock3 size={16} aria-hidden="true" />
        <span className="expiring-todos__title">即将到期</span>
      </div>
      <div className="expiring-todos__list">
        {expiringTodos.map((todo) => (
          <TodoItem
            key={todo.id}
            todo={todo}
            variant="longterm"
            onToggle={toggleTodo}
            onUpdateDueDate={updateDueDate}
            onUpdatePriority={updatePriority}
            onUpdateUrgent={updateUrgent}
            onDelete={removeTodo}
            onAddToToday={addToToday}
          />
        ))}
      </div>
    </div>
  );
}
