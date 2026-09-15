/**
 * 记录卡片组件
 * 支持显示子记录，每条记录都有独立时间戳
 * 超过10行的内容默认收起
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ImagePlus, MoreVertical, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { parseContent } from '../../utils/parseContent';
import { formatDate } from '../../utils/formatDate';
import IconButton from '../common/IconButton';
import ConfirmDialog from '../common/ConfirmDialog';
import { AttachmentImageDialog, AttachmentImageThumbnail } from '../common/AttachmentImagePreview';
import {
  deleteAttachment,
  downloadAttachment,
  getAttachmentStatus,
  reprocessAttachment,
  uploadEntryAttachment,
} from '../../api/attachments';
import { deleteEntry, updateEntry } from '../../api/entries';
import type {
  AttachmentResponse,
  EntryResponse,
  EntryWithChildrenResponse,
} from '../../types/api';

const ATTACHMENT_ACCEPT =
  'image/jpeg,image/png,image/webp,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,.jpg,.jpeg,.png,.webp,.pdf,.pptx';

interface EntryCardProps {
  entry: EntryWithChildrenResponse;
  canEditDeleteOwnEntries?: boolean;
  onAppendBelow?: (entry: EntryWithChildrenResponse) => void;
  onAttachmentDeleted?: (entryId: number, attachmentId: number) => void;
  onAttachmentsAdded?: (entryId: number, attachments: AttachmentResponse[]) => void;
  onEntryUpdated?: (entry: EntryResponse) => void;
  onEntryDeleted?: (entryId: number) => void;
  onMutationForbidden?: () => void | Promise<void>;
}

function mutationErrorMessage(err: unknown): string {
  const code = (err as { code?: string } | null)?.code;
  if (code === 'ENTRY_MUTATION_FORBIDDEN') {
    return '未获得修改或删除原记录的权限';
  }
  if (err instanceof Error && err.message) return err.message;
  return '操作失败';
}

const LINE_COLLAPSE_THRESHOLD = 10;
const ATTACHMENT_STATUS_POLL_MS = 2500;
const ATTACHMENT_STATUS_MAX_POLLS = 72;

interface CollapsibleTextProps {
  content: string;
  lines: number;
}

function CollapsibleText({ content, lines }: CollapsibleTextProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  if (lines <= LINE_COLLAPSE_THRESHOLD) {
    return <p className="growth-entry-body">{content}</p>;
  }

  const displayContent = isExpanded ? content : content.split('\n').slice(0, LINE_COLLAPSE_THRESHOLD).join('\n');

  return (
    <div>
      <p className="growth-entry-body">{displayContent}</p>
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="growth-link-button growth-expand-button"
      >
        {isExpanded ? '收起' : `展开剩余 ${lines - LINE_COLLAPSE_THRESHOLD} 行`}
      </button>
    </div>
  );
}

function formatFileSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.max(1, Math.round(size / 1024))}KB`;
}

function statusText(status: AttachmentResponse['status']): string {
  const map = {
    uploaded: '已上传',
    processing: '处理中',
    indexed: '可检索',
    failed: '处理失败',
  };
  return map[status] || status;
}

function attachmentStatusClass(status: AttachmentResponse['status']): string {
  switch (status) {
    case 'processing':
      return 'growth-attachment-status growth-attachment-status--processing';
    case 'indexed':
      return 'growth-attachment-status growth-attachment-status--indexed';
    case 'failed':
      return 'growth-attachment-status growth-attachment-status--failed';
    case 'uploaded':
      return 'growth-attachment-status growth-attachment-status--uploaded';
    default:
      return 'growth-attachment-status';
  }
}

function ocrStatusClass(ocrStatus?: string | null): string {
  switch (ocrStatus) {
    case 'succeeded':
      return 'growth-attachment-ocr growth-attachment-ocr--success';
    case 'empty':
    case 'unavailable':
    case 'failed':
      return 'growth-attachment-ocr growth-attachment-ocr--warn';
    case 'disabled':
    default:
      return 'growth-attachment-ocr growth-attachment-ocr--neutral';
  }
}

/** User-facing OCR visibility only; never show provider or internal fields. */
function ocrStatusText(ocrStatus?: string | null): string | null {
  switch (ocrStatus) {
    case 'succeeded':
      return '图片文字已识别';
    case 'empty':
      return '未识别到图片文字';
    case 'unavailable':
    case 'failed':
      return '图片文字暂不可检索';
    case 'disabled':
      return '当前仅按图片信息检索';
    default:
      return null;
  }
}

function truncateErrorMessage(message: string, max = 72): string {
  const cleaned = message.replace(/\s+/g, ' ').trim();
  if (!cleaned) return '';
  return cleaned.length > max ? `${cleaned.slice(0, max)}…` : cleaned;
}

function isPendingAttachmentStatus(status: AttachmentResponse['status']): boolean {
  return status === 'uploaded' || status === 'processing';
}

function isImageAttachment(attachment: AttachmentResponse): boolean {
  return (
    attachment.mime_type.startsWith('image/') ||
    /\.(jpg|jpeg|png|webp|gif)$/i.test(attachment.original_filename)
  );
}

function AttachmentList({
  entryId,
  attachments,
  onDeleted,
  onAdded,
}: {
  entryId: number;
  attachments: AttachmentResponse[];
  onDeleted?: (attachment: AttachmentResponse) => void;
  onAdded?: (attachments: AttachmentResponse[]) => void;
}) {
  const [items, setItems] = useState<AttachmentResponse[]>(attachments || []);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [reprocessingId, setReprocessingId] = useState<number | null>(null);
  const [previewAttachment, setPreviewAttachment] = useState<AttachmentResponse | null>(null);
  const [actionErrors, setActionErrors] = useState<Record<number, string>>({});
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pollCountRef = useRef(0);

  useEffect(() => {
    setItems(attachments || []);
    pollCountRef.current = 0;
  }, [attachments]);

  const pendingIdsKey = useMemo(
    () =>
      items
        .filter((item) => isPendingAttachmentStatus(item.status))
        .map((item) => item.id)
        .sort((a, b) => a - b)
        .join(','),
    [items],
  );

  useEffect(() => {
    if (!pendingIdsKey) {
      pollCountRef.current = 0;
      return;
    }

    let cancelled = false;
    const pendingIds = pendingIdsKey.split(',').map((id) => Number(id));

    const pollOnce = async () => {
      if (cancelled) return;
      if (pollCountRef.current >= ATTACHMENT_STATUS_MAX_POLLS) return;
      pollCountRef.current += 1;

      const updates = await Promise.all(
        pendingIds.map(async (id) => {
          try {
            return await getAttachmentStatus(id);
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;

      setItems((prev) =>
        prev.map((item) => {
          const next = updates.find((row) => row && row.id === item.id);
          return next || item;
        }),
      );
    };

    void pollOnce();
    const timer = window.setInterval(() => {
      void pollOnce();
    }, ATTACHMENT_STATUS_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pendingIdsKey]);

  const handleAddFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0 || uploading) return;
    const files = Array.from(fileList);
    setUploading(true);
    setUploadError('');
    const uploaded: AttachmentResponse[] = [];
    try {
      for (const file of files) {
        uploaded.push(await uploadEntryAttachment(entryId, file));
      }
      if (uploaded.length > 0) {
        pollCountRef.current = 0;
        setItems((prev) => [...prev, ...uploaded]);
        onAdded?.(uploaded);
      }
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : '添加附件失败，请稍后重试');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDelete = async (attachment: AttachmentResponse) => {
    if (!window.confirm(`删除附件「${attachment.original_filename}」？`)) return;
    setDeletingId(attachment.id);
    try {
      await deleteAttachment(attachment.id);
      setItems((prev) => prev.filter((item) => item.id !== attachment.id));
      onDeleted?.(attachment);
    } finally {
      setDeletingId(null);
    }
  };

  const handleReprocess = async (attachment: AttachmentResponse) => {
    setReprocessingId(attachment.id);
    setActionErrors((prev) => ({ ...prev, [attachment.id]: '' }));
    try {
      const updated = await reprocessAttachment(attachment.id);
      pollCountRef.current = 0;
      setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
    } catch (error) {
      const message = error instanceof Error ? error.message : '重新识别失败，请稍后重试';
      setActionErrors((prev) => ({ ...prev, [attachment.id]: message }));
    } finally {
      setReprocessingId(null);
    }
  };

  return (
    <div className="growth-entry-attachments">
      <div className="growth-entry-image-grid">
        {items.filter(isImageAttachment).map((attachment) => (
          <AttachmentImageThumbnail
            key={attachment.id}
            attachmentId={attachment.id}
            filename={attachment.original_filename}
            className="growth-entry-image-preview"
            onOpen={() => setPreviewAttachment(attachment)}
          />
        ))}
      </div>
      <div className="growth-entry-attachment-toolbar">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ATTACHMENT_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          onChange={(event) => void handleAddFiles(event.target.files)}
        />
        <button
          type="button"
          className="growth-link-button"
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading}
          aria-busy={uploading}
        >
          <ImagePlus size={14} aria-hidden="true" />
          {uploading ? '上传中…' : '添加图片'}
        </button>
        {uploadError ? <span className="growth-attachment-error">{uploadError}</span> : null}
      </div>
      {items.map((attachment) => {
        const reindexRequired =
          isImageAttachment(attachment) &&
          attachment.status === 'indexed' &&
          attachment.ocr_reindex_required === true;
        const errorHint =
          actionErrors[attachment.id] ||
          (attachment.status === 'failed' && attachment.error_message
            ? truncateErrorMessage(attachment.error_message)
            : '');
        const ocrHint =
          isImageAttachment(attachment) && attachment.status === 'indexed'
            ? (reindexRequired ? '需重新识别' : ocrStatusText(attachment.ocr_status))
            : null;
        return (
          <div
            key={attachment.id}
            className="growth-attachment-chip"
          >
            <button
              type="button"
              className="growth-attachment-name"
              title={isImageAttachment(attachment) ? `查看原图：${attachment.original_filename}` : `下载：${attachment.original_filename}`}
              onClick={() => {
                if (isImageAttachment(attachment)) setPreviewAttachment(attachment);
                else void downloadAttachment(attachment);
              }}
            >
              <span className="growth-attachment-name-text">{attachment.original_filename}</span>
            </button>
            <span className="growth-attachment-meta">{formatFileSize(attachment.file_size)}</span>
            <span
              className={attachmentStatusClass(attachment.status)}
              title={errorHint || ocrHint || statusText(attachment.status)}
            >
              {statusText(attachment.status)}
            </span>
            {ocrHint ? (
              <span
                className={reindexRequired ? 'growth-attachment-ocr growth-attachment-ocr--warn' : ocrStatusClass(attachment.ocr_status)}
                title={ocrHint}
              >
                {ocrHint}
              </span>
            ) : null}
            {errorHint ? (
              <span className="growth-attachment-error" title={attachment.error_message || errorHint}>
                {errorHint}
              </span>
            ) : null}
            {reindexRequired ? (
              <IconButton
                icon={RefreshCw}
                label="重新识别图片文字"
                variant="ghost"
                className="growth-attachment-reprocess"
                onClick={() => handleReprocess(attachment)}
                disabled={reprocessingId === attachment.id}
                aria-busy={reprocessingId === attachment.id}
              />
            ) : null}
            <IconButton
              icon={Trash2}
              label="删除附件"
              variant="ghost"
              className="growth-attachment-delete"
              onClick={() => handleDelete(attachment)}
              disabled={deletingId === attachment.id}
            />
          </div>
        );
      })}
      <AttachmentImageDialog
        attachmentId={previewAttachment?.id ?? null}
        filename={previewAttachment?.original_filename ?? '原始图片'}
        open={previewAttachment !== null}
        onClose={() => setPreviewAttachment(null)}
      />
    </div>
  );
}

interface EntryMutationMenuProps {
  entryId: number;
  content: string;
  isRoot: boolean;
  hasChildren: boolean;
  canEditDeleteOwnEntries: boolean;
  onEntryUpdated?: (entry: EntryResponse) => void;
  onEntryDeleted?: (entryId: number) => void;
  onMutationForbidden?: () => void | Promise<void>;
}

function EntryMutationMenu({
  entryId,
  content,
  isRoot,
  hasChildren,
  canEditDeleteOwnEntries,
  onEntryUpdated,
  onEntryDeleted,
  onMutationForbidden,
}: EntryMutationMenuProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editContent, setEditContent] = useState(content);
  const [editError, setEditError] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [menuOpen]);

  if (!canEditDeleteOwnEntries) return null;

  const deleteMessage = isRoot || hasChildren
    ? '删除后不可恢复。将同时删除全部子记录与附件。'
    : '删除后不可恢复。将同时删除该记录的附件（如有）。';

  const handleSave = async () => {
    if (saving) return;
    const next = editContent.trim();
    if (!next) {
      setEditError('内容不能为空');
      return;
    }
    setSaving(true);
    setEditError('');
    try {
      const updated = await updateEntry(entryId, { content: next });
      onEntryUpdated?.(updated);
      setEditOpen(false);
      setMenuOpen(false);
    } catch (err) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 403) {
        setEditError('未获得修改或删除原记录的权限');
        await onMutationForbidden?.();
      } else {
        setEditError(mutationErrorMessage(err));
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (deleting) return;
    setDeleting(true);
    setDeleteError('');
    try {
      await deleteEntry(entryId);
      setDeleteOpen(false);
      setMenuOpen(false);
      onEntryDeleted?.(entryId);
    } catch (err) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 403) {
        setDeleteError('未获得修改或删除原记录的权限');
        await onMutationForbidden?.();
      } else {
        setDeleteError(mutationErrorMessage(err));
      }
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="growth-entry-menu" ref={menuRef}>
      <IconButton
        icon={MoreVertical}
        label="记录操作"
        variant="ghost"
        className="growth-entry-menu-trigger"
        onClick={() => setMenuOpen((open) => !open)}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
      />
      {menuOpen && (
        <div className="growth-entry-menu-panel" role="menu">
          <button
            type="button"
            className="growth-entry-menu-item"
            role="menuitem"
            onClick={() => {
              setEditContent(content);
              setEditError('');
              setEditOpen(true);
              setMenuOpen(false);
            }}
          >
            <Pencil size={14} aria-hidden="true" />
            <span>编辑记录</span>
          </button>
          <button
            type="button"
            className="growth-entry-menu-item growth-entry-menu-item--danger"
            role="menuitem"
            onClick={() => {
              setDeleteError('');
              setDeleteOpen(true);
              setMenuOpen(false);
            }}
          >
            <Trash2 size={14} aria-hidden="true" />
            <span>删除记录</span>
          </button>
        </div>
      )}

      {editOpen && (
        <div className="growth-entry-edit-root" role="presentation">
          <div
            className="growth-entry-edit-overlay"
            aria-hidden="true"
            onClick={() => {
              if (!saving) setEditOpen(false);
            }}
          />
          <div
            className="growth-entry-edit-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="编辑记录"
          >
            <h2>编辑记录</h2>
            <textarea
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              rows={10}
              disabled={saving}
              aria-label="记录内容"
            />
            {editError ? (
              <div className="growth-entry-edit-error" role="alert">
                {editError}
              </div>
            ) : null}
            <div className="growth-entry-edit-actions">
              <button type="button" onClick={() => setEditOpen(false)} disabled={saving}>
                取消
              </button>
              <button
                type="button"
                className="growth-entry-edit-primary"
                onClick={() => void handleSave()}
                disabled={saving}
              >
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={deleteOpen}
        title="删除记录"
        message={deleteMessage}
        confirmLabel="删除"
        cancelLabel="取消"
        destructive
        loading={deleting}
        error={deleteError}
        onConfirm={() => void handleDelete()}
        onClose={() => {
          if (!deleting) setDeleteOpen(false);
        }}
      />
    </div>
  );
}

export default function EntryCard({
  entry,
  canEditDeleteOwnEntries = false,
  onAppendBelow,
  onAttachmentDeleted,
  onAttachmentsAdded,
  onEntryUpdated,
  onEntryDeleted,
  onMutationForbidden,
}: EntryCardProps) {
  const { title } = parseContent(entry.content);
  const [isChildrenExpanded, setIsChildrenExpanded] = useState(true);

  // 主记录正文（去掉标题行）
  const mainBody = entry.content.replace(/^.*?\n/, '').trim() || entry.content;
  const mainBodyLines = mainBody.split('\n').length;

  // 判断是否有子记录
  const hasChildren = entry.children && entry.children.length > 0;

  return (
    <article className="growth-entry-card">
      <div className="growth-entry-card-top">
        <div className="growth-entry-date">{formatDate(entry.created_at)}</div>
        <span className="growth-entry-label">{entry.label_name}</span>
        <EntryMutationMenu
          entryId={entry.id}
          content={entry.content}
          isRoot
          hasChildren={Boolean(hasChildren)}
          canEditDeleteOwnEntries={canEditDeleteOwnEntries}
          onEntryUpdated={onEntryUpdated}
          onEntryDeleted={onEntryDeleted}
          onMutationForbidden={onMutationForbidden}
        />
      </div>

      {/* 主记录标题 */}
      {title && (
        <h3 className="growth-entry-title">{title}</h3>
      )}

      {/* 主记录内容 */}
      <div className="growth-entry-main">
        <CollapsibleText content={mainBody} lines={mainBodyLines} />
        <AttachmentList
          entryId={entry.id}
          attachments={entry.attachments || []}
          onDeleted={(attachment) => onAttachmentDeleted?.(entry.id, attachment.id)}
          onAdded={(attachments) => onAttachmentsAdded?.(entry.id, attachments)}
        />
      </div>

      {/* 子记录列表 */}
      {hasChildren && isChildrenExpanded && (
        <div className="growth-entry-children">
          {entry.children.map((child) => {
            const childBody = child.content.replace(/^.*?\n/, '').trim() || child.content;
            const childLines = childBody.split('\n').length;
            return (
              <div key={child.id} className="growth-entry-child">
                <div className="growth-entry-child-top">
                  <div className="growth-entry-date">
                    {formatDate(child.created_at)}
                  </div>
                  <EntryMutationMenu
                    entryId={child.id}
                    content={child.content}
                    isRoot={false}
                    hasChildren={false}
                    canEditDeleteOwnEntries={canEditDeleteOwnEntries}
                    onEntryUpdated={onEntryUpdated}
                    onEntryDeleted={onEntryDeleted}
                    onMutationForbidden={onMutationForbidden}
                  />
                </div>
                <CollapsibleText content={childBody} lines={childLines} />
                <AttachmentList
                  entryId={child.id}
                  attachments={child.attachments || []}
                  onDeleted={(attachment) => onAttachmentDeleted?.(child.id, attachment.id)}
                  onAdded={(attachments) => onAttachmentsAdded?.(child.id, attachments)}
                />
              </div>
            );
          })}
        </div>
      )}

      {/* 操作按钮区域 */}
      <div className="growth-entry-actions">
        <div className="growth-entry-action-group">
          {onAppendBelow && (
            <button
              type="button"
              onClick={() => onAppendBelow(entry)}
              className="growth-link-button"
            >
              记录在这条下面
            </button>
          )}
          {hasChildren && (
            <button
              type="button"
              onClick={() => setIsChildrenExpanded(!isChildrenExpanded)}
              className="growth-secondary-link-button"
            >
              {isChildrenExpanded ? '收起' : `展开 ${entry.children.length} 条`}
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
