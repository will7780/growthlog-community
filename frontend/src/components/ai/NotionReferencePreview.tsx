import { ArrowLeft, X } from 'lucide-react';
import type { ReferenceItem } from '../../types/api';
import IconButton from '../common/IconButton';
import { safeNotionUrl } from './NotionOpenLink';

interface NotionReferencePreviewProps {
  reference: ReferenceItem;
  loading: boolean;
  error: string;
  openUrl?: string | null;
  onClose: () => void;
  variant: 'desktop' | 'mobile';
}

const SYNC_LABEL: Record<string, string> = {
  indexed: '已同步',
  partial: '部分同步',
  pending: '同步中',
  processing: '同步中',
  failed: '同步失败',
};

export default function NotionReferencePreview({
  reference,
  loading,
  error,
  openUrl,
  onClose,
  variant,
}: NotionReferencePreviewProps) {
  const CloseIcon = variant === 'desktop' ? X : ArrowLeft;
  const breadcrumb = typeof reference.metadata?.breadcrumb === 'string' ? reference.metadata.breadcrumb : '';
  const syncStatus = typeof reference.metadata?.sync_status === 'string' ? reference.metadata.sync_status : '';
  const safeUrl = safeNotionUrl(openUrl);

  return (
    <aside
      className={`growth-ai-reference-detail growth-ai-reference-detail--${variant}`}
      data-testid="notion-reference-preview"
      role={variant === 'desktop' ? 'dialog' : undefined}
      aria-label={variant === 'desktop' ? 'Notion 引用详情' : undefined}
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
          <span>引用 Notion</span>
          <h2>{reference.title || 'Notion 页面'}</h2>
        </div>
      </header>
      <div className="growth-ai-reference-detail__scroll">
        {loading && <div className="growth-ai-reference-detail__state">正在加载页面…</div>}
        {error && <div className="growth-ai-reference-detail__state growth-ai-reference-detail__state--error">{error}</div>}
        {!loading && !error && (
          <>
            {safeUrl && <a className="entry-sheet-btn entry-sheet-btn--ghost" href={safeUrl} target="_blank" rel="noopener noreferrer">在 Notion 中打开</a>}
            {breadcrumb ? (
              <section className="growth-ai-reference-detail__section">
                <h3>路径</h3>
                <p className="growth-ai-reference-detail__meta">{breadcrumb}</p>
              </section>
            ) : null}
            {syncStatus ? (
              <section className="growth-ai-reference-detail__section">
                <h3>同步状态</h3>
                <p className="growth-ai-reference-detail__meta">{SYNC_LABEL[syncStatus] || syncStatus}</p>
              </section>
            ) : null}
            <section className="growth-ai-reference-detail__section">
              <h3>命中片段</h3>
              <p>{reference.snippet || '没有可展示的片段'}</p>
            </section>
          </>
        )}
      </div>
    </aside>
  );
}
