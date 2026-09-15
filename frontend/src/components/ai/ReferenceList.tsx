/**
 * 扁平引用列表 — 桌面/移动共用 referenceDisplay 契约，无 card 套 card。
 */
import type { ReferenceItem } from '../../types/api';
import { getReferenceCardKey, getReferenceOriginKey, referenceCardTitle } from './referenceDisplay';

function sourceLabel(ref: ReferenceItem): string {
  if (ref.source_type === 'attachment_chunk') return '附件';
  if (ref.source_type === 'knowledge_source') return '知识库';
  if (ref.source_type === 'todo') return '小要事';
  if (ref.source_type === 'notion_page') return 'Notion';
  return '记录';
}

function sourceBreadcrumb(ref: ReferenceItem): string {
  const value = ref.metadata?.breadcrumb;
  return typeof value === 'string' ? value.trim() : '';
}

interface ReferenceListProps {
  references: ReferenceItem[];
  variant?: 'desktop' | 'mobile';
  onSelect?: (ref: ReferenceItem, index: number) => void;
  activeKey?: string | null;
}

export default function ReferenceList({
  references,
  variant = 'mobile',
  onSelect,
  activeKey = null,
}: ReferenceListProps) {
  const deduped: ReferenceItem[] = [];
  const seen = new Map<string, number>();
  references.forEach((ref, idx) => {
    const origin = getReferenceOriginKey(ref, idx);
    const prev = seen.get(origin);
    if (prev == null) {
      seen.set(origin, deduped.length);
      deduped.push(ref);
      return;
    }
    if (ref.source_type === 'attachment_chunk' && deduped[prev].source_type !== 'attachment_chunk') {
      deduped[prev] = ref;
    }
  });

  if (deduped.length === 0) return null;

  const rootClass =
    variant === 'desktop' ? 'gl-ref-list gl-ref-list--desktop' : 'gl-ref-list gl-ref-list--mobile';

  return (
    <div className={rootClass} aria-label="引用列表">
      <div className="gl-ref-list__head">引用 {deduped.length} 个来源</div>
      <ul className="gl-ref-list__items">
        {deduped.map((ref, idx) => {
          const key = getReferenceCardKey(ref, idx);
          const title = referenceCardTitle(ref);
          const active = activeKey != null && activeKey === key;
          return (
            <li key={key}>
              <button
                type="button"
                className={`gl-ref-row${active ? ' gl-ref-row--active' : ''}`}
                data-testid="ai-reference-row"
                title={title}
                aria-label={title}
                onClick={() => onSelect?.(ref, idx)}
              >
                <span className="gl-ref-row__main">
                  <span className="gl-ref-row__title">{title}</span>
                  <span className="gl-ref-row__meta">
                    {sourceLabel(ref)}
                    {sourceBreadcrumb(ref) ? ` · ${sourceBreadcrumb(ref)}` : ref.label_name ? ` · ${ref.label_name}` : ''}
                  </span>
                </span>
                {ref.relevance_score != null && (
                  <span className="gl-ref-row__score">{Math.round(ref.relevance_score * 100)}%</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
