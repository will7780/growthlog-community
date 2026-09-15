import { useEffect, useState } from 'react';
import { ArrowLeft, ExternalLink, X } from 'lucide-react';
import type { EntryWithChildrenResponse, ReferenceItem } from '../../types/api';
import { parseContent } from '../../utils/parseContent';
import IconButton from '../common/IconButton';
import { AttachmentImageDialog, AttachmentImageThumbnail } from '../common/AttachmentImagePreview';
import { referenceCardTitle } from './referenceDisplay';

interface ReferenceDetailProps {
  reference: ReferenceItem;
  entry: EntryWithChildrenResponse | null;
  loading: boolean;
  error: string;
  onClose: () => void;
  onOpenEntry?: (entryId: number) => void;
  annotationText?: string;
  annotationLoading?: boolean;
  onGenerateAnnotation?: () => void;
  appendText?: string;
  appendLoading?: boolean;
  onAppendTextChange?: (value: string) => void;
  onAppend?: () => void;
  variant?: 'desktop' | 'mobile';
}

function entryBody(content: string): string {
  const parsed = parseContent(content);
  return parsed.body.trim() || parsed.title;
}

function displayDate(value?: string | null): string {
  return value ? value.slice(0, 10) : '';
}

function referenceFilename(reference: ReferenceItem): string {
  const filename = reference.metadata && typeof reference.metadata.filename === 'string'
    ? reference.metadata.filename.trim()
    : '';
  return filename || reference.title.split('（', 1)[0].trim();
}

function isImageReference(reference: ReferenceItem): boolean {
  const mimeType = reference.metadata && typeof reference.metadata.mime_type === 'string'
    ? reference.metadata.mime_type.toLowerCase()
    : '';
  return (
    mimeType.startsWith('image/') ||
    /\.(jpg|jpeg|png|webp)$/i.test(referenceFilename(reference))
  );
}

export default function ReferenceDetail({
  reference,
  entry,
  loading,
  error,
  onClose,
  onOpenEntry,
  annotationText = '',
  annotationLoading = false,
  onGenerateAnnotation,
  appendText = '',
  appendLoading = false,
  onAppendTextChange,
  onAppend,
  variant = 'desktop',
}: ReferenceDetailProps) {
  const parsed = entry ? parseContent(entry.content) : null;
  const isFile = reference.source_type === 'attachment_chunk' || reference.source_type === 'knowledge_source';
  const CloseIcon = variant === 'desktop' ? X : ArrowLeft;
  const filename = referenceFilename(reference);
  const imageAttachmentId =
    reference.attachment_id != null && isImageReference(reference) ? reference.attachment_id : null;
  const [imageOpen, setImageOpen] = useState(false);
  const hitEntryId = reference.entry_id;
  const isChildHit = Boolean(
    entry && (entry.id !== hitEntryId || reference.parent_id || reference.parent_snippet),
  );
  const parentSnippet = reference.parent_snippet?.trim() || '';
  const parentTitle = reference.parent_title?.trim() || '';

  useEffect(() => {
    setImageOpen(false);
  }, [imageAttachmentId]);

  return (
    <>
      <aside
        className={`growth-ai-reference-detail growth-ai-reference-detail--${variant}`}
      role={variant === 'desktop' ? 'dialog' : undefined}
      aria-label={variant === 'desktop' ? '引用详情' : undefined}
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
          <span>引用{isFile ? '附件' : '记录'}</span>
          <h2>{referenceCardTitle(reference)}</h2>
        </div>
      </header>

      <div className="growth-ai-reference-detail__scroll">
        {loading && <div className="growth-ai-reference-detail__state">正在加载完整记录...</div>}
        {error && <div className="growth-ai-reference-detail__state growth-ai-reference-detail__state--error">{error}</div>}

        {imageAttachmentId != null && (
          <section className="growth-ai-reference-detail__section">
            <h3>原始图片</h3>
            <AttachmentImageThumbnail
              attachmentId={imageAttachmentId}
              filename={filename || '原始图片'}
              className="growth-ai-reference-detail__image"
              onOpen={() => setImageOpen(true)}
            />
          </section>
        )}

        {isFile && reference.snippet && (
          <section className="growth-ai-reference-detail__section">
            <h3>命中片段</h3>
            <p className="growth-ai-reference-detail__snippet">{reference.snippet}</p>
          </section>
        )}

        {!loading && parentSnippet && (!entry || entry.parent_id != null) && (
          <section className="growth-ai-reference-detail__section">
            <h3>所属父记录</h3>
            {parentTitle ? <p className="growth-ai-reference-detail__meta"><span>{parentTitle}</span></p> : null}
            <p className="growth-ai-reference-detail__body">{parentSnippet}</p>
            {onOpenEntry && reference.root_entry_id != null && (
              <button
                type="button"
                className="growth-ai-reference-detail__open"
                onClick={() => onOpenEntry(reference.root_entry_id as number)}
              >
                <ExternalLink size={14} aria-hidden="true" />
                打开父记录
              </button>
            )}
          </section>
        )}

        {entry && !loading && (
          <>
            <section className="growth-ai-reference-detail__section">
              <div className="growth-ai-reference-detail__meta">
                <span>{entry.label_name}</span>
                <span>{displayDate(entry.created_at)}</span>
                {isChildHit ? <span>含追加语境</span> : null}
              </div>
              <h3>{parsed?.title || referenceCardTitle(reference)}</h3>
              <p className="growth-ai-reference-detail__body">{entryBody(entry.content)}</p>
              {onOpenEntry && (
                <button
                  type="button"
                  className="growth-ai-reference-detail__open"
                  onClick={() => onOpenEntry(entry.id)}
                >
                  <ExternalLink size={14} aria-hidden="true" />
                  打开原记录
                </button>
              )}
            </section>

            {entry.children?.length > 0 && (
              <section className="growth-ai-reference-detail__section">
                <h3>已有追加 {entry.children.length} 条</h3>
                <div className="growth-ai-reference-detail__children">
                  {entry.children.map((child) => (
                    <article
                      key={child.id}
                      className={child.id === hitEntryId ? 'growth-ai-reference-detail__child--hit' : undefined}
                    >
                      <time>{displayDate(child.created_at)}</time>
                      <p>{entryBody(child.content)}</p>
                      {child.id === hitEntryId ? <span className="growth-ai-reference-detail__hit-badge">本次命中</span> : null}
                    </article>
                  ))}
                </div>
              </section>
            )}
          </>
        )}

        {onGenerateAnnotation && (
          <section className="growth-ai-reference-detail__section growth-ai-reference-detail__tool">
            <h3>AI 批注</h3>
            <p>先生成预览，不会修改原文。</p>
            {annotationText && <div className="growth-ai-reference-detail__annotation">{annotationText}</div>}
            <button type="button" onClick={onGenerateAnnotation} disabled={annotationLoading || !entry}>
              {annotationLoading ? '生成中...' : '生成 AI 批注'}
            </button>
          </section>
        )}

        {onAppend && onAppendTextChange && (
          <section className="growth-ai-reference-detail__section growth-ai-reference-detail__tool">
            <label htmlFor={`reference-append-${variant}`}>在这条下面追加</label>
            <textarea
              id={`reference-append-${variant}`}
              value={appendText}
              onChange={(event) => onAppendTextChange(event.target.value)}
              placeholder="把新的想法或补充记录在这条下面..."
              rows={4}
              disabled={appendLoading || !entry}
            />
            <button type="button" onClick={onAppend} disabled={!appendText.trim() || appendLoading || !entry}>
              {appendLoading ? '追加中...' : '追加到这条记录'}
            </button>
          </section>
        )}
      </div>
      </aside>
      <AttachmentImageDialog
        attachmentId={imageAttachmentId}
        filename={filename || '原始图片'}
        open={imageOpen}
        onClose={() => setImageOpen(false)}
      />
    </>
  );
}
