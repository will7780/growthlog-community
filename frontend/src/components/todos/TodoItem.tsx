import { useEffect, useId, useRef, useState } from 'react';
import type { ButtonHTMLAttributes } from 'react';
import { flushSync } from 'react-dom';
import {
  CalendarDays,
  CalendarMinus,
  CalendarPlus,
  ChevronDown,
  ChevronRight,
  ListPlus,
  GripVertical,
  MoreVertical,
  Trash2,
  Zap,
} from 'lucide-react';
import type {
  TodoChildCreateRequest,
  TodoPriority,
  TodoResponse,
} from '../../types/todo';
import IconButton from '../common/IconButton';
import ConfirmDialog from '../common/ConfirmDialog';
import TodoChildForm from './TodoChildForm';
import { TODO_PRIORITIES, TODO_PRIORITY_LABELS } from './todoPriority';
import {
  beijingDaysUntil,
  beijingTodayYmd,
  formatBeijingDueLabel,
} from '../../utils/beijingDate';

interface TodoItemProps {
  todo: TodoResponse;
  variant?: 'default' | 'today' | 'longterm';
  hasVisibleChildren?: boolean;
  collapsed?: boolean;
  showAncestorPath?: boolean;
  dragHandleProps?: ButtonHTMLAttributes<HTMLButtonElement>;
  dragHandleRef?: (node: HTMLButtonElement | null) => void;
  dragging?: boolean;
  onToggleCollapse?: () => void;
  onToggle: (id: number, isDone: boolean, completionNote?: string | null) => Promise<void>;
  onUpdateDueDate: (id: number, dueDate: string | null) => Promise<void>;
  onUpdatePriority: (id: number, priority: TodoPriority) => Promise<void>;
  onUpdateUrgent?: (id: number, isUrgent: boolean) => Promise<void>;
  onDelete: (id: number, expectedDescendantCount: number) => Promise<void>;
  onAddChild?: (parentId: number, data: TodoChildCreateRequest) => Promise<unknown>;
  onAddToToday?: (id: number) => Promise<void>;
  onRemoveFromToday?: (id: number) => Promise<void>;
}

const NOTE_MAX = 1000;

export default function TodoItem({
  todo,
  variant = 'default',
  hasVisibleChildren = false,
  collapsed = false,
  showAncestorPath = false,
  dragHandleProps,
  dragHandleRef,
  dragging = false,
  onToggleCollapse,
  onToggle,
  onUpdateDueDate,
  onUpdatePriority,
  onUpdateUrgent,
  onDelete,
  onAddChild,
  onAddToToday,
  onRemoveFromToday,
}: TodoItemProps) {
  const [showDatePicker, setShowDatePicker] = useState(false);
  const [showMenu, setShowMenu] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [busy, setBusy] = useState(false);
  const [completeOpen, setCompleteOpen] = useState(false);
  const [completionNote, setCompletionNote] = useState('');
  const [completing, setCompleting] = useState(false);
  const [completeError, setCompleteError] = useState('');
  const [childOpen, setChildOpen] = useState(false);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const childButtonRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  useEffect(() => {
    if (!completeOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const timer = window.setTimeout(() => noteRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !completing) {
        event.preventDefault();
        setCompleteOpen(false);
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = previousOverflow;
    };
  }, [completeOpen, completing]);

  const closeChildForm = () => {
    flushSync(() => setChildOpen(false));
    window.setTimeout(() => {
      childButtonRef.current?.focus({ preventScroll: true });
    }, 0);
  };

  const handleDelete = async () => {
    setDeleting(true);
    setDeleteError('');
    try {
      await onDelete(todo.id, todo.descendant_count);
      setDeleteOpen(false);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : '删除失败，请稍后重试');
    } finally {
      setDeleting(false);
    }
  };

  const handleCheckboxChange = () => {
    if (todo.is_done) {
      void onToggle(todo.id, false);
      return;
    }
    setCompletionNote('');
    setCompleteError('');
    setCompleteOpen(true);
  };

  const handleCompleteConfirm = async () => {
    if (completing) return;
    setCompleting(true);
    setCompleteError('');
    try {
      const note = completionNote.trim();
      await onToggle(todo.id, true, note ? note.slice(0, NOTE_MAX) : null);
      setCompleteOpen(false);
    } catch (err) {
      setCompleteError(err instanceof Error ? err.message : '完成失败，请稍后重试');
    } finally {
      setCompleting(false);
    }
  };

  const isStep = todo.parent_id !== null;
  const daysUntil = beijingDaysUntil(todo.due_date);
  const expiring = !isStep && todo.is_expiring_soon && !todo.is_done;
  const priorityClass = `todo-priority--${todo.priority.toLowerCase()}`;
  const deleteMessage = todo.descendant_count
    ? `确认删除“${todo.content}”及其 ${todo.descendant_count} 个后代任务吗？删除后无法恢复。`
    : `确认删除“${todo.content}”吗？删除后无法恢复。`;

  return (
    <div
      className={'todo-item ' + (todo.is_done ? 'todo-item--done ' : '') +
        (expiring ? 'todo-item--expiring ' : '') +
        (!isStep && todo.is_urgent ? 'todo-item--urgent ' : '') +
        (isStep ? 'todo-item--step ' : 'todo-item--' + todo.priority.toLowerCase()) +
        (dragging ? ' todo-item--dragging' : '')}
      data-depth={todo.depth}
    >
      {isStep && dragHandleProps ? (
        <button
          ref={dragHandleRef}
          type="button"
          {...dragHandleProps}
          className="todo-drag-handle"
          aria-label="拖动调整步骤顺序"
          title="拖动调整步骤顺序"
        >
          <GripVertical size={17} aria-hidden="true" />
        </button>
      ) : null}
      <button
        type="button"
        className={`todo-tree-toggle ${hasVisibleChildren ? '' : 'todo-tree-toggle--placeholder'}`}
        onClick={onToggleCollapse}
        disabled={!hasVisibleChildren}
        aria-label={collapsed ? '展开子任务' : '折叠子任务'}
        aria-expanded={hasVisibleChildren ? !collapsed : undefined}
      >
        {hasVisibleChildren ? (
          collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />
        ) : null}
      </button>

      <label className="todo-checkbox">
        <input
          type="checkbox"
          checked={todo.is_done}
          onChange={handleCheckboxChange}
          disabled={completing}
        />
        <span className="todo-checkbox__box" aria-hidden="true">
          {todo.is_done ? <span className="todo-checkbox__check">✓</span> : null}
        </span>
        <span className="sr-only">{todo.is_done ? '标记为未完成' : '标记为完成'}</span>
      </label>

      <div className="todo-content">
        {showAncestorPath && todo.ancestor_titles.length ? (
          <div className="todo-ancestor-path" title={todo.ancestor_titles.join(' / ')}>
            {todo.ancestor_titles.join(' / ')}
          </div>
        ) : null}
        <span className={`todo-content__text ${todo.is_done ? 'todo-content__text--done' : ''}`}>
          {todo.content}
        </span>
        {todo.descendant_count ? (
          <span className="todo-child-progress">
            {todo.descendant_count - todo.incomplete_descendant_count}/{todo.descendant_count} 个子任务完成
          </span>
        ) : null}
        {todo.is_overdue && !todo.is_done ? <span className="todo-overdue-badge">已过期</span> : null}
        {todo.is_done && todo.completion_note ? (
          <p className="todo-completion-note">{todo.completion_note}</p>
        ) : null}
      </div>

      <div className="todo-meta">
        {!isStep ? <>
        {onUpdateUrgent ? (
          <button
            type="button"
            className={`todo-urgent-toggle ${todo.is_urgent ? 'todo-urgent-toggle--active' : ''}`}
            aria-pressed={todo.is_urgent}
            aria-label={todo.is_urgent ? '取消紧急' : '标为紧急'}
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void onUpdateUrgent(todo.id, !todo.is_urgent).finally(() => setBusy(false));
            }}
          >
            <Zap size={14} aria-hidden="true" />
            <span>紧急</span>
          </button>
        ) : null}

        <label className={`todo-priority-select ${priorityClass}`} title="修改优先级">
          <span className="sr-only">优先级</span>
          <select
            value={todo.priority}
            onChange={(event) => void onUpdatePriority(todo.id, event.target.value as TodoPriority)}
            aria-label={`当前优先级 ${todo.priority}，修改优先级`}
          >
            {TODO_PRIORITIES.map((item) => (
              <option key={item} value={item}>{item} {TODO_PRIORITY_LABELS[item]}</option>
            ))}
          </select>
        </label>

        {todo.due_date ? (
          <span className={`todo-due ${expiring || todo.is_overdue ? 'todo-due--warning' : ''}`}>
            {formatBeijingDueLabel(todo.due_date)}
            {daysUntil !== null && daysUntil >= 0 && daysUntil <= 3 && !todo.is_done ? (
              <span className="todo-due__days">{daysUntil === 0 ? '今天到期' : `剩 ${daysUntil} 天`}</span>
            ) : null}
          </span>
        ) : null}

        <IconButton
          icon={CalendarDays}
          label={todo.due_date ? '修改截止日期' : '添加截止日期'}
          size="sm"
          variant="ghost"
          onClick={() => setShowDatePicker((value) => !value)}
          className="todo-date-btn"
        />

        {variant === 'longterm' && onAddToToday && !todo.is_done ? (
          <IconButton
            icon={CalendarPlus}
            label="加入今日计划"
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void onAddToToday(todo.id).finally(() => setBusy(false));
            }}
          />
        ) : null}

        {variant === 'today' && onRemoveFromToday ? (
          <IconButton
            icon={CalendarMinus}
            label="移出今日计划"
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void onRemoveFromToday(todo.id).finally(() => setBusy(false));
            }}
          />
        ) : null}

        </> : null}

        {todo.depth < 3 && onAddChild ? (
          <button
            ref={childButtonRef}
            type="button"
            className="todo-add-child"
            title="添加子任务"
            aria-label={`给“${todo.content}”添加子任务`}
            aria-expanded={childOpen}
            onClick={() => setChildOpen((value) => !value)}
          >
            <ListPlus size={17} aria-hidden="true" />
          </button>
        ) : null}

        <div className="todo-more">
          <IconButton
            icon={MoreVertical}
            label="更多操作"
            size="sm"
            variant="ghost"
            aria-expanded={showMenu}
            onClick={() => setShowMenu((value) => !value)}
          />
          {showMenu ? (
            <div className="todo-more-menu" role="menu">
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setShowMenu(false);
                  setDeleteError('');
                  setDeleteOpen(true);
                }}
              >
                <Trash2 size={16} aria-hidden="true" />
                删除
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {showDatePicker && !isStep ? (
        <div className="todo-date-picker">
          <input
            type="date"
            value={todo.due_date || ''}
            onChange={(event) => {
              void onUpdateDueDate(todo.id, event.target.value || null);
              setShowDatePicker(false);
            }}
            min={beijingTodayYmd()}
            aria-label="选择截止日期"
          />
          {todo.due_date ? (
            <button
              type="button"
              className="todo-date-clear"
              onClick={() => {
                void onUpdateDueDate(todo.id, null);
                setShowDatePicker(false);
              }}
            >
              清除日期
            </button>
          ) : null}
        </div>
      ) : null}

      {childOpen && onAddChild ? (
        <TodoChildForm
          parent={todo}
          onSubmit={async (data: TodoChildCreateRequest) => { await onAddChild(todo.id, data); }}
          onClose={closeChildForm}
        />
      ) : null}

      <ConfirmDialog
        open={deleteOpen}
        title={todo.descendant_count ? '删除任务及子任务' : '删除小要事'}
        message={deleteMessage}
        confirmLabel="删除"
        destructive
        loading={deleting}
        error={deleteError}
        onClose={() => {
          if (!deleting) setDeleteOpen(false);
        }}
        onConfirm={handleDelete}
      />

      {completeOpen ? (
        <div className="todo-complete-root" role="presentation">
          <div
            className="todo-complete-overlay"
            aria-hidden="true"
            onClick={() => {
              if (!completing) setCompleteOpen(false);
            }}
          />
          <div className="todo-complete-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
            <h2 id={titleId}>完成小要事</h2>
            <p className="todo-complete-lead">有什么收获和心得？</p>
            <p className="todo-complete-task" title={todo.content}>{todo.content}</p>
            <textarea
              ref={noteRef}
              value={completionNote}
              onChange={(event) => setCompletionNote(event.target.value.slice(0, NOTE_MAX))}
              rows={4}
              maxLength={NOTE_MAX}
              disabled={completing}
              placeholder="可选填写，不写也可以直接完成"
              aria-label="收获和心得"
            />
            <div className="todo-complete-meta"><span>{completionNote.length}/{NOTE_MAX}</span></div>
            {completeError ? <div className="todo-complete-error" role="alert">{completeError}</div> : null}
            <div className="todo-complete-actions">
              <button type="button" onClick={() => setCompleteOpen(false)} disabled={completing}>取消</button>
              <button
                type="button"
                className="todo-complete-primary"
                onClick={() => void handleCompleteConfirm()}
                disabled={completing}
              >
                {completing ? '完成中...' : '完成'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}