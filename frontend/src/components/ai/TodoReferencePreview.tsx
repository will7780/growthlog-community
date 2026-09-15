import { ArrowLeft, X } from 'lucide-react';
import type { CSSProperties } from 'react';
import type { ReferenceItem } from '../../types/api';
import type { TodoReferenceNode } from '../../api/ai';
import IconButton from '../common/IconButton';

interface TodoReferencePreviewProps {
  reference: ReferenceItem;
  tree: TodoReferenceNode[];
  loading: boolean;
  error: string;
  onClose: () => void;
  variant: 'desktop' | 'mobile';
}

function statusLabel(status: TodoReferenceNode['status']): string {
  return status === 'completed' ? '已完成' : '未完成';
}

export default function TodoReferencePreview({
  reference,
  tree,
  loading,
  error,
  onClose,
  variant,
}: TodoReferencePreviewProps) {
  const CloseIcon = variant === 'desktop' ? X : ArrowLeft;
  return (
    <aside
      className={`growth-ai-reference-detail growth-ai-reference-detail--${variant}`}
      data-testid="todo-reference-preview"
      role={variant === 'desktop' ? 'dialog' : undefined}
      aria-label={variant === 'desktop' ? '小要事引用详情' : undefined}
    >
      <header className="growth-ai-reference-detail__header">
        <IconButton
          icon={CloseIcon}
          label={variant === 'desktop' ? '关闭引用详情' : '返回聊天'}
          size="sm"
          variant="ghost"
          onClick={onClose}
        />
        <div>
          <span>引用小要事</span>
          <h2>{reference.title || '小要事'}</h2>
        </div>
      </header>
      <div className="growth-ai-reference-detail__scroll">
        {loading && <div className="growth-ai-reference-detail__state">正在加载任务树...</div>}
        {error && <div className="growth-ai-reference-detail__state growth-ai-reference-detail__state--error">{error}</div>}
        {!loading && !error && reference.metadata?.path_titles && Array.isArray(reference.metadata.path_titles) ? (
          <section className="growth-ai-reference-detail__section">
            <h3>任务路径</h3>
            <p className="growth-ai-reference-detail__meta">{reference.metadata.path_titles.join(' / ')}</p>
          </section>
        ) : null}
        {!loading && !error && (
          <section className="growth-ai-reference-detail__section">
            <h3>任务树</h3>
            <ol className="ai-todo-reference-tree">
              {tree.map((node, index) => (
                <li
                  key={`${index}:${node.depth}:${node.content}`}
                  className={`ai-todo-reference-tree__node${node.is_hit ? ' ai-todo-reference-tree__node--hit' : ''}`}
                  style={{ '--todo-depth': node.depth } as CSSProperties}
                >
                  <div className="ai-todo-reference-tree__title">
                    <span>{node.content}</span>
                    <span className="ai-todo-reference-tree__status">{statusLabel(node.status)}</span>
                  </div>
                  <div className="ai-todo-reference-tree__meta">
                    {node.is_step ? (
                      <span>步骤 {(node.sort_order ?? 0) + 1}</span>
                    ) : (
                      <>
                        {node.priority ? <span>{node.priority}</span> : null}
                        {node.urgent ? <span>紧急</span> : null}
                        {node.due_date ? <span>截止 {node.due_date}</span> : null}
                      </>
                    )}
                    {node.completed_at ? <span>完成 {node.completed_at.slice(0, 16).replace('T', ' ')}</span> : null}
                  </div>
                  {node.completion_note ? <p className="ai-todo-reference-tree__note">{node.completion_note}</p> : null}
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    </aside>
  );
}
