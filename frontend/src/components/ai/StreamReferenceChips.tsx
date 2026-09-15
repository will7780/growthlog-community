/**
 * 检索员/讲解员回答下方的来源条 —— 复用 gl-ref-list 视觉，但数据源是脱敏后的
 * StreamReferenceItem（没有 entry_id，点击只打开轻量预览，不拉完整记录）。
 */
import type { StreamReferenceItem } from '../../api/aiStream';
import NotionOpenLink from './NotionOpenLink';
import ReferenceSourceBadge from './ReferenceSourceBadge';

interface StreamReferenceChipsProps {
  references: StreamReferenceItem[];
  activeToken?: string | null;
  onSelect: (ref: StreamReferenceItem) => void;
  variant?: 'desktop' | 'mobile';
  sessionId: string;
}

export default function StreamReferenceChips({
  references,
  activeToken = null,
  onSelect,
  variant = 'desktop',
  sessionId,
}: StreamReferenceChipsProps) {
  if (!references || references.length === 0) return null;
  const rootClass = variant === 'desktop' ? 'gl-ref-list gl-ref-list--desktop' : 'gl-ref-list gl-ref-list--mobile';
  return (
    <div className={rootClass} aria-label="引用列表">
      <div className="gl-ref-list__head">引用 {references.length} 个来源</div>
      <ul className="gl-ref-list__items">
        {references.map((ref) => {
          const active = activeToken != null && activeToken === ref.ref_token;
          const title = ref.title || '参考记录';
          return (
            <li key={ref.ref_token}>
              <button
                type="button"
                className={`gl-ref-row${active ? ' gl-ref-row--active' : ''}`}
                title={title}
                aria-label={title}
                onClick={() => onSelect(ref)}
              >
                <span className="gl-ref-row__main">
                  <span className="gl-ref-row__title">〔{ref.display_index}〕 {title}</span>
                  <span className="gl-ref-row__meta"><ReferenceSourceBadge sourceType={ref.source_type} /></span>
                </span>
              </button>
              {ref.source_type === 'notion_page' && <NotionOpenLink sessionId={sessionId} refToken={ref.ref_token} />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
