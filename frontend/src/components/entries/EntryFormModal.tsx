/**
 * 移动端新建记录 sheet（可访问 dialog + 焦点陷阱）
 */
import { useState, useEffect, useRef, useId, useCallback, type FormEvent } from 'react';
import { X, Trash2 } from 'lucide-react';
import type { LabelResponse } from '../../types/api';
import IconButton from '../common/IconButton';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('disabled') && el.getAttribute('aria-hidden') !== 'true'
  );
}

interface EntryFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (labelCode: string, content: string, files?: File[]) => Promise<void>;
  labels: LabelResponse[];
  initialLabelCode?: string;
  appendTarget?: { entryId: number; labelCode: string; title: string } | null;
  onCancelAppend?: () => void;
}

export default function EntryFormModal({
  isOpen,
  onClose,
  onSubmit,
  labels,
  initialLabelCode,
  appendTarget = null,
  onCancelAppend,
}: EntryFormModalProps) {
  const titleId = useId();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [labelCode, setLabelCode] = useState(initialLabelCode || '');
  const [files, setFiles] = useState<File[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const dialogRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const seededRef = useRef(false);

  const focusInitial = useCallback(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const nodes = getFocusable(dialog);
    if (nodes.length > 0) {
      (contentRef.current && nodes.includes(contentRef.current)
        ? contentRef.current
        : nodes[0]
      ).focus();
      return;
    }
    dialog.focus();
  }, []);

  // 打开：播种、锁滚、焦点进入；关闭：恢复焦点
  useEffect(() => {
    if (!isOpen) {
      seededRef.current = false;
      return;
    }
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    if (!seededRef.current) {
      setTitle('');
      setContent('');
      setLabelCode(
        appendTarget?.labelCode ||
          initialLabelCode ||
          labels.find((l) => l.code !== 'todo')?.code ||
          ''
      );
      setFiles([]);
      setError('');
      seededRef.current = true;
    }
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const t = window.setTimeout(focusInitial, 0);
    return () => {
      window.clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      const prev = previouslyFocused.current;
      if (prev && typeof prev.focus === 'function') {
        prev.focus();
      }
    };
  }, [isOpen, initialLabelCode, labels, appendTarget, focusInitial]);

  // Escape + Tab 焦点循环（仅 dialog 内）
  useEffect(() => {
    if (!isOpen) return;

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (!loading) {
          e.preventDefault();
          e.stopPropagation();
          onClose();
        }
        return;
      }

      if (e.key !== 'Tab') return;
      const dialog = dialogRef.current;
      if (!dialog) return;

      const nodes = getFocusable(dialog);
      if (nodes.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }

      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement as HTMLElement | null;

      if (e.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !dialog.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [isOpen, loading, onClose]);

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!content.trim() || !labelCode || loading) return;
    setLoading(true);
    setError('');
    try {
      const fullContent = title.trim() ? `${title.trim()}\n${content.trim()}` : content.trim();
      await onSubmit(labelCode, fullContent, files);
      seededRef.current = false;
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : '发布失败，请稍后重试';
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) return null;

  const entryLabels = labels.filter((l) => l.code !== 'todo');

  return (
    <div className="entry-sheet-root" role="presentation">
      <div
        className="entry-sheet-overlay"
        onClick={() => {
          if (!loading) onClose();
        }}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        className="entry-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="entry-sheet-handle" aria-hidden="true" />
        <header className="entry-sheet-header">
          <h2 id={titleId} className="entry-sheet-title">
            {appendTarget ? '追加记录' : '新建记录'}
          </h2>
          <IconButton
            icon={X}
            label="关闭新建记录"
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!loading) onClose();
            }}
            disabled={loading}
          />
        </header>

        <form className="entry-sheet-body" onSubmit={handleSubmit}>
          {appendTarget && (
            <p className="entry-sheet-append-hint">
              追加到：{appendTarget.title || `记录 #${appendTarget.entryId}`}
              {onCancelAppend && (
                <button
                  type="button"
                  className="entry-sheet-link"
                  onClick={onCancelAppend}
                  disabled={loading}
                >
                  取消追加
                </button>
              )}
            </p>
          )}
          <label className="entry-sheet-label" htmlFor="entry-sheet-title">
            标题（可选）
          </label>
          <input
            id="entry-sheet-title"
            type="text"
            placeholder="记录标题"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="entry-sheet-input"
            disabled={loading}
          />

          <label className="entry-sheet-label" htmlFor="entry-sheet-content">
            内容
          </label>
          <textarea
            id="entry-sheet-content"
            ref={contentRef}
            placeholder="这一刻想记录什么..."
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="entry-sheet-textarea"
            rows={4}
            required
            disabled={loading}
          />

          <label className="entry-sheet-label" htmlFor="entry-sheet-label">
            标签
          </label>
          <select
            id="entry-sheet-label"
            value={labelCode}
            onChange={(e) => setLabelCode(e.target.value)}
            className="entry-sheet-select"
            required
            disabled={loading}
          >
            {entryLabels.map((label) => (
              <option key={label.code} value={label.code}>
                {label.name}
              </option>
            ))}
          </select>

          <label className="entry-sheet-label" htmlFor="entry-sheet-files">
            附件（可选）
          </label>
          <input
            id="entry-sheet-files"
            type="file"
            multiple
            accept="image/jpeg,image/png,image/webp,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,.jpg,.jpeg,.png,.webp,.pdf,.pptx"
            onChange={(e) => setFiles(Array.from(e.target.files || []))}
            className="entry-sheet-file"
            disabled={loading}
          />

          {files.length > 0 && (
            <ul className="entry-sheet-file-list" aria-label={`已选 ${files.length} 个文件`}>
              {files.map((file, index) => (
                <li key={`${file.name}-${index}`} className="entry-sheet-file-item">
                  <span className="entry-sheet-file-name" title={file.name}>
                    {file.name}
                  </span>
                  <IconButton
                    icon={Trash2}
                    label={`移除 ${file.name}`}
                    size="sm"
                    variant="ghost"
                    onClick={() => removeFile(index)}
                    disabled={loading}
                  />
                </li>
              ))}
              <li className="entry-sheet-file-count">共 {files.length} 个文件</li>
            </ul>
          )}

          {error && (
            <div className="entry-sheet-error" role="alert">
              {error}
            </div>
          )}

          <footer className="entry-sheet-footer">
            <button
              type="button"
              className="entry-sheet-btn entry-sheet-btn--ghost"
              onClick={() => {
                if (!loading) onClose();
              }}
              disabled={loading}
            >
              取消
            </button>
            <button
              type="submit"
              className="entry-sheet-btn entry-sheet-btn--primary"
              disabled={!content.trim() || !labelCode || loading}
            >
              {loading ? '发布中...' : '发布'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
