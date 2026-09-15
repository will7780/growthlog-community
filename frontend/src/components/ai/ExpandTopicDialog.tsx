/**
 * 补充来源主题输入：桌面 dialog / 移动 sheet。
 * 主题 trim 后 2～200 字；Enter 提交、Escape 关闭；关闭后恢复触发按钮焦点。
 */
import { useEffect, useId, useRef, useState } from 'react';
import { Loader2, X } from 'lucide-react';

const TOPIC_MIN = 2;
const TOPIC_MAX = 200;

interface ExpandTopicDialogProps {
  open: boolean;
  variant?: 'desktop' | 'mobile';
  loading?: boolean;
  error?: string;
  initialTopic?: string;
  onClose: () => void;
  onSubmit: (topic: string) => void | Promise<void>;
  returnFocusRef?: React.MutableRefObject<HTMLElement | null>;
}

export default function ExpandTopicDialog({
  open,
  variant = 'desktop',
  loading = false,
  error = '',
  initialTopic = '',
  onClose,
  onSubmit,
  returnFocusRef,
}: ExpandTopicDialogProps) {
  const titleId = useId();
  const descId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [topic, setTopic] = useState(initialTopic);
  const [localError, setLocalError] = useState('');
  const prefix = variant === 'mobile' ? 'ai-mp-expand-topic' : 'growth-ai-expand-topic';

  useEffect(() => {
    if (!open) return;
    setTopic(initialTopic);
    setLocalError('');
    const previous = document.body.style.overflow;
    if (variant === 'mobile') document.body.style.overflow = 'hidden';
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      document.body.style.overflow = previous;
      window.clearTimeout(t);
    };
  }, [open, initialTopic, variant]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!loading) onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, loading, onClose]);

  useEffect(() => {
    if (!open || !panelRef.current) return;
    const root = panelRef.current;
    const focusable = () =>
      Array.from(
        root.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), [href], textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute('disabled') && el.tabIndex !== -1);

    const onTab = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const nodes = focusable();
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    root.addEventListener('keydown', onTab);
    return () => root.removeEventListener('keydown', onTab);
  }, [open]);

  useEffect(() => {
    if (open) return;
    const node = returnFocusRef?.current;
    if (node && typeof node.focus === 'function') {
      window.setTimeout(() => node.focus(), 0);
    }
  }, [open, returnFocusRef]);

  if (!open) return null;

  const submit = () => {
    const trimmed = topic.trim();
    if (trimmed.length < TOPIC_MIN || trimmed.length > TOPIC_MAX) {
      setLocalError('请输入 2～200 字的补充主题');
      return;
    }
    setLocalError('');
    void onSubmit(trimmed);
  };

  const body = (
    <div
      ref={panelRef}
      className={`${prefix}__panel`}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descId}
      data-testid="expand-topic-dialog"
    >
      <div className={`${prefix}__head`}>
        <h3 id={titleId}>补充来源</h3>
        <button
          type="button"
          className={`${prefix}__close`}
          onClick={onClose}
          disabled={loading}
          aria-label="关闭"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      <p id={descId} className={`${prefix}__desc`}>
        想补充哪方面的资料？
      </p>
      <input
        ref={inputRef}
        className={`${prefix}__input`}
        type="text"
        value={topic}
        maxLength={TOPIC_MAX}
        disabled={loading}
        placeholder="例如：TDD 的实践方法、项目部署风险、与当前观点相反的记录"
        data-testid="expand-topic-input"
        onChange={(e) => setTopic(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (!loading) submit();
          }
        }}
      />
      {(localError || error) && (
        <div className={`${prefix}__error`} data-testid="expand-topic-error">
          {localError || error}
        </div>
      )}
      <div className={`${prefix}__actions`}>
        <button type="button" className={`${prefix}__btn`} onClick={onClose} disabled={loading}>
          取消
        </button>
        <button
          type="button"
          className={`${prefix}__btn ${prefix}__btn--primary`}
          onClick={submit}
          disabled={loading}
          data-testid="expand-topic-submit"
        >
          {loading ? <Loader2 size={14} className={`${prefix}__spin`} aria-hidden="true" /> : null}
          查找来源
        </button>
      </div>
    </div>
  );

  if (variant === 'mobile') {
    return (
      <div className={`${prefix}__overlay`} data-testid="expand-topic-overlay">
        <div className={`${prefix}__sheet`}>{body}</div>
      </div>
    );
  }

  return (
    <div className={`${prefix}__backdrop`} data-testid="expand-topic-overlay" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()}>{body}</div>
    </div>
  );
}
