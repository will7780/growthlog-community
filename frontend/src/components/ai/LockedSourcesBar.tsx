/**
 * 讲解员固定来源管理：输入框上方摘要条 + 可展开清单。
 * 只支持查看预览与补充来源；不提供删除（append-only）。
 */
import { useEffect, useId, useRef, useState } from 'react';
import { BookMarked, ChevronDown, ChevronUp, Loader2, Plus, X } from 'lucide-react';
import type { CurrentLockedSourcesResponse, LockedSourceMember } from '../../api/ai';
import type { StreamReferenceItem } from '../../api/aiStream';

interface LockedSourcesBarProps {
  sources: CurrentLockedSourcesResponse | null;
  loading?: boolean;
  expanding?: boolean;
  error?: string;
  disabled?: boolean;
  /** Opens topic dialog/sheet; does not call expand API directly. */
  onExpand: () => void;
  onPreview: (member: StreamReferenceItem) => void;
  variant?: 'desktop' | 'mobile';
  expandButtonRef?: React.MutableRefObject<HTMLButtonElement | null>;
}

function toStreamRef(member: LockedSourceMember): StreamReferenceItem {
  return {
    display_index: member.display_index,
    ref_token: member.ref_token,
    source_type: member.source_type === 'attachment' ? 'attachment' : member.source_type === 'todo' ? 'todo' : member.source_type === 'notion_page' ? 'notion_page' : 'entry',
    title: member.title,
    snippet: member.snippet,
  };
}

export default function LockedSourcesBar({
  sources,
  loading = false,
  expanding = false,
  error = '',
  disabled = false,
  onExpand,
  onPreview,
  variant = 'desktop',
  expandButtonRef,
}: LockedSourcesBarProps) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const localExpandRef = useRef<HTMLButtonElement>(null);
  const expandRef = expandButtonRef || localExpandRef;
  const prefix = variant === 'mobile' ? 'ai-mp-locked-sources' : 'growth-ai-locked-sources';

  useEffect(() => {
    if (!open || variant !== 'mobile') return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const t = window.setTimeout(() => closeBtnRef.current?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = previous;
      window.clearTimeout(t);
      window.removeEventListener('keydown', onKey);
    };
  }, [open, variant]);

  if (!sources || !sources.version || sources.member_count <= 0) {
    return null;
  }

  const summary = (
    <div className={`${prefix}__bar`} data-testid="locked-sources-bar">
      <button
        type="button"
        className={`${prefix}__summary`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={titleId}
      >
        <BookMarked size={14} aria-hidden="true" />
        <span>
          当前来源 · v{sources.version} · {sources.member_count} 条
        </span>
        {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
      </button>
      <button
        ref={expandRef}
        type="button"
        className={`${prefix}__expand`}
        onClick={() => onExpand()}
        disabled={disabled || expanding || !sources.can_expand || sources.stale}
        data-testid="locked-sources-expand"
      >
        {expanding ? <Loader2 size={14} className={`${prefix}__spin`} aria-hidden="true" /> : <Plus size={14} aria-hidden="true" />}
        补充来源
      </button>
    </div>
  );

  const listBody = (
    <div className={`${prefix}__panel`} id={titleId}>
      <div className={`${prefix}__note`}>{sources.append_only_note}</div>
      {loading && <div className={`${prefix}__status`}>加载中…</div>}
      {error && <div className={`${prefix}__error`}>{error}</div>}
      <ul className={`${prefix}__list`}>
        {sources.members.map((member) => (
          <li key={`${member.display_index}:${member.ref_token.slice(0, 12)}`}>
            <button
              type="button"
              className={`${prefix}__item`}
              onClick={() => onPreview(toStreamRef(member))}
            >
              <span className={`${prefix}__item-title`}>
                〔{member.display_index}〕 {member.title || '参考来源'}
              </span>
              <span className={`${prefix}__item-meta`}>
                {member.source_type === 'attachment' ? '附件' : member.source_type === 'todo' ? '小要事' : member.source_type === 'notion_page' ? 'Notion' : '记录'}
                {member.snippet ? ` · ${member.snippet}` : ''}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );

  if (variant === 'mobile') {
    return (
      <div className={prefix}>
        {summary}
        {open && (
          <div className={`${prefix}__overlay`} role="presentation">
            <div
              className={`${prefix}__sheet`}
              role="dialog"
              aria-modal="true"
              aria-labelledby={`${titleId}-title`}
            >
              <div className={`${prefix}__sheet-head`}>
                <h3 id={`${titleId}-title`}>固定来源</h3>
                <button
                  type="button"
                  className={`${prefix}__sheet-close`}
                  ref={closeBtnRef}
                  onClick={() => setOpen(false)}
                >
                  <X size={16} aria-hidden="true" />
                  关闭
                </button>
              </div>
              {listBody}
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={prefix}>
      {summary}
      {open && listBody}
    </div>
  );
}
