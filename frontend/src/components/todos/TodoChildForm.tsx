import { useEffect, useRef, useState } from 'react';
import type { TodoChildCreateRequest, TodoResponse } from '../../types/todo';

interface TodoChildFormProps {
  parent: TodoResponse;
  onSubmit: (data: TodoChildCreateRequest) => Promise<void>;
  onClose: () => void;
}

export default function TodoChildForm({
  parent,
  onSubmit,
  onClose,
}: TodoChildFormProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = async () => {
    const value = content.trim();
    if (!value || submitting) return;
    setSubmitting(true);
    setError('');
    try {
      await onSubmit({ content: value });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加子任务失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="todo-child-form"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !submitting) {
          event.preventDefault();
          onClose();
        }
        if (event.key === 'Enter' && !event.shiftKey && event.target === inputRef.current) {
          event.preventDefault();
          void submit();
        }
      }}
    >
      <div className="todo-child-form__quick">
        <input
          ref={inputRef}
          value={content}
          onChange={(event) => setContent(event.target.value.slice(0, 500))}
          maxLength={500}
          placeholder={'添加下一步到“' + parent.content + '”下面'}
          aria-label="子任务步骤内容"
          disabled={submitting}
        />
      </div>
      {error ? <div className="todo-child-form__error" role="alert">{error}</div> : null}
      <div className="todo-child-form__actions">
        <button type="button" onClick={onClose} disabled={submitting}>取消</button>
        <button
          type="button"
          className="todo-child-form__primary"
          onClick={() => void submit()}
          disabled={submitting || !content.trim()}
        >
          {submitting ? '添加中…' : '添加步骤'}
        </button>
      </div>
    </div>
  );
}