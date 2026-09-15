import { useState } from 'react';
import { Zap } from 'lucide-react';
import type { TodoPriority } from '../../types/todo';
import { TODO_PRIORITIES, TODO_PRIORITY_LABELS } from './todoPriority';
import { beijingAddDaysYmd, beijingTodayYmd } from '../../utils/beijingDate';

export interface TodoFormSubmitPayload {
  content: string;
  dueDate: string | null;
  priority: TodoPriority;
  isUrgent: boolean;
  addToToday: boolean;
}

interface TodoFormProps {
  onSubmit: (payload: TodoFormSubmitPayload) => Promise<void>;
}

export default function TodoForm({ onSubmit }: TodoFormProps) {
  const [content, setContent] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [priority, setPriority] = useState<TodoPriority>('P4');
  const [isUrgent, setIsUrgent] = useState(false);
  const [addToToday, setAddToToday] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!content.trim()) return;
    setLoading(true);
    setError('');
    try {
      await onSubmit({
        content: content.trim(),
        dueDate: dueDate || null,
        priority,
        isUrgent,
        addToToday,
      });
      setContent('');
      setDueDate('');
      setPriority('P4');
      setIsUrgent(false);
      setAddToToday(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  const setQuickDate = (days: number) => {
    setDueDate(beijingAddDaysYmd(days));
  };

  return (
    <form className="todo-form" onSubmit={handleSubmit}>
      <input
        type="text"
        className="todo-form__input"
        placeholder="此刻想记录的小要事..."
        value={content}
        onChange={(event) => setContent(event.target.value)}
        disabled={loading}
      />
      <fieldset className="todo-priority-picker">
        <legend>优先级</legend>
        <div className="todo-priority-picker__options">
          {TODO_PRIORITIES.map((item) => (
            <button
              key={item}
              type="button"
              className={`todo-priority-option todo-priority-option--${item.toLowerCase()} ${
                item === priority ? 'todo-priority-option--active' : ''
              }`}
              aria-pressed={item === priority}
              title={`${item} ${TODO_PRIORITY_LABELS[item]}`}
              onClick={() => setPriority(item)}
              disabled={loading}
            >
              <span aria-hidden="true" />
              {item}
            </button>
          ))}
        </div>
      </fieldset>
      <div className="todo-form__flags">
        <button
          type="button"
          className={`todo-urgent-toggle ${isUrgent ? 'todo-urgent-toggle--active' : ''}`}
          aria-pressed={isUrgent}
          aria-label={isUrgent ? '取消紧急' : '标为紧急'}
          title={isUrgent ? '紧急（与优先级独立）' : '标为紧急（与优先级独立）'}
          onClick={() => setIsUrgent((value) => !value)}
          disabled={loading}
        >
          <Zap size={14} aria-hidden="true" />
          <span>紧急</span>
        </button>
        <label className="todo-form__today-check">
          <input
            type="checkbox"
            checked={addToToday}
            onChange={(event) => setAddToToday(event.target.checked)}
            disabled={loading}
          />
          <span>同时加入今日计划</span>
        </label>
      </div>
      <div className="todo-form__row">
        <input
          type="date"
          className="todo-form__date"
          value={dueDate}
          onChange={(event) => setDueDate(event.target.value)}
          min={beijingTodayYmd()}
          disabled={loading}
          aria-label="截止日期"
        />
        <div className="todo-form__quick-dates">
          <button type="button" onClick={() => setQuickDate(0)}>今天</button>
          <button type="button" onClick={() => setQuickDate(1)}>明天</button>
          <button type="button" onClick={() => setQuickDate(7)}>一周</button>
        </div>
      </div>
      {error && <div className="todo-form__error" role="alert">{error}</div>}
      <button type="submit" className="todo-form__submit" disabled={loading || !content.trim()}>
        {loading ? '添加中...' : '添加'}
      </button>
    </form>
  );
}
