/**
 * 共享引用卡片展示语义（桌面 AIAssistantPanel / 移动 AIMPContent）。
 * 禁止在 UI 文本中暴露 opaque key。
 */
export interface ReferenceLike {
  source_type?: string | null;
  source_id?: number | null;
  attachment_id?: number | null;
  entry_id?: number | null;
  title?: string | null;
  page_no?: number | null;
  slide_no?: number | null;
  metadata?: Record<string, unknown> | null;
}

/** 仅用于 React key / 选中态，不得渲染到可见文案 */
export function getReferenceCardKey(ref: ReferenceLike, index = 0): string {
  const meta = ref.metadata || {};
  const filename = typeof meta.filename === 'string' ? meta.filename.trim() : '';
  return [
    ref.source_type || 'entry',
    ref.attachment_id ?? ref.source_id ?? ref.entry_id ?? 'unknown',
    ref.page_no ?? '',
    ref.slide_no ?? '',
    filename,
    index,
  ].join(':');
}

/** 同一附件页的 knowledge 镜像和 attachment 引用应视为同一证据。 */
export function getReferenceOriginKey(ref: ReferenceLike, index = 0): string {
  if (ref.attachment_id != null) {
    return `attachment:${ref.attachment_id}:${ref.page_no ?? ''}:${ref.slide_no ?? ''}`;
  }
  if (ref.entry_id != null) return `entry:${ref.entry_id}`;
  return getReferenceCardKey(ref, index);
}

export function referenceCardTitle(ref: ReferenceLike): string {
  const meta = ref.metadata || {};
  const filename = typeof meta.filename === 'string' ? meta.filename.trim() : '';
  const isFileRef =
    ref.source_type === 'attachment_chunk' ||
    ref.source_type === 'knowledge_source' ||
    Boolean(filename);
  if (isFileRef && filename) {
    let loc = '';
    if (ref.page_no != null) loc = ` · 第 ${ref.page_no} 页`;
    else if (ref.slide_no != null) loc = ` · 第 ${ref.slide_no} 张`;
    return `${filename}${loc}`;
  }
  const title = (ref.title || '').trim();
  if (title && !/^(entry|attachment_chunk|knowledge_source):/.test(title)) {
    return title;
  }
  return '参考记录';
}
