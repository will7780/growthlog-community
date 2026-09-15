/**
 * R11.3-Fix：完整引用预览。
 * 通过 JWT + body 中的 opaque ref_token 拉取完整原记录/附件，
 * 复用 ReferenceDetail + AttachmentImagePreview；桌面左侧 / 移动 bottom sheet。
 * 关闭后不改动聊天滚动与未发送输入（由父组件保留）。
 */
import { useEffect, useState } from 'react';
import type { EntryWithChildrenResponse, ReferenceItem } from '../../types/api';
import type { StreamReferenceItem } from '../../api/aiStream';
import { previewStreamReference } from '../../api/ai';
import type { TodoReferenceNode } from '../../api/ai';
import ReferenceDetail from './ReferenceDetail';
import TodoReferencePreview from './TodoReferencePreview';
import NotionReferencePreview from './NotionReferencePreview';

interface StreamReferencePreviewProps {
  reference: StreamReferenceItem;
  sessionId: string;
  onClose: () => void;
  variant?: 'desktop' | 'mobile';
}

export default function StreamReferencePreview({
  reference,
  sessionId,
  onClose,
  variant = 'desktop',
}: StreamReferencePreviewProps) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detailRef, setDetailRef] = useState<ReferenceItem | null>(null);
  const [entry, setEntry] = useState<EntryWithChildrenResponse | null>(null);
  const [todoTree, setTodoTree] = useState<TodoReferenceNode[]>([]);
  const [openUrl, setOpenUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setDetailRef(null);
    setEntry(null);
    setTodoTree([]);
    setOpenUrl(null);
    previewStreamReference(sessionId, reference.ref_token)
      .then((data) => {
        if (cancelled) return;
        setDetailRef(data.reference);
        setEntry(data.entry);
        setTodoTree(data.todo_tree || []);
        setOpenUrl(data.open_url || (typeof data.reference?.metadata?.open_url === 'string' ? data.reference.metadata.open_url : null));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : '引用预览加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, reference.ref_token]);

  const fallbackRef: ReferenceItem = detailRef || {
    entry_id: 0,
    title: reference.title,
    label_name: '',
    created_at: reference.created_at ?? null,
    snippet: reference.snippet,
    relevance_score: 0,
    source_type: reference.source_type === 'attachment' ? 'attachment_chunk' : reference.source_type,
  };

  if (fallbackRef.source_type === 'notion_page') {
    return (
      <NotionReferencePreview
        reference={fallbackRef}
        loading={loading}
        error={error}
        openUrl={openUrl}
        onClose={onClose}
        variant={variant}
      />
    );
  }

  if (reference.source_type === 'todo') {
    return (
      <TodoReferencePreview
        reference={fallbackRef}
        tree={todoTree}
        loading={loading}
        error={error}
        onClose={onClose}
        variant={variant}
      />
    );
  }

  return (
    <ReferenceDetail
      reference={fallbackRef}
      entry={entry}
      loading={loading}
      error={error}
      onClose={onClose}
      variant={variant}
    />
  );
}
