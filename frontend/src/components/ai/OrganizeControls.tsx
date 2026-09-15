/**
 * R11.6 整理师范围栏 + 分组可编辑 diff 预览（桌面/移动共用）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getEntries } from '../../api/entries';
import { getLabels } from '../../api/labels';
import {
  confirmOrganize,
  previewOrganizeScope,
  type OrganizeDays,
  type OrganizeDiffItem,
  type OrganizePreviewResponse,
  type OrganizeScopePreviewResponse,
  type OrganizeSourceDisplay,
} from '../../api/ai';
import type { EntryWithChildrenResponse, LabelResponse } from '../../types/api';
import { organizeErrorMessage } from '../../utils/aiSafeErrors';
import { formatDate } from '../../utils/formatDate';
import { parseContent } from '../../utils/parseContent';

const ENTRY_PAGE_SIZE = 50;

const DAY_OPTIONS: Array<{ value: OrganizeDays; label: string }> = [
  { value: 7, label: '7天' },
  { value: 30, label: '30天' },
  { value: 90, label: '90天' },
  { value: null, label: '全部' },
];

const ACTION_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'create_summary', label: '摘要' },
  { value: 'suggest_tag', label: '标签' },
  { value: 'create_action_plan', label: '行动计划' },
  { value: 'rewrite_content', label: '改写' },
  { value: 'memory_note', label: '个性设置' },
];

const ACTION_GROUP_LABEL: Record<string, string> = {
  create_summary: '摘要',
  suggest_tag: '标签建议',
  create_action_plan: '行动计划',
  rewrite_content: '改写建议',
  memory_note: '个性设置草稿',
};

export interface OrganizeScopeState {
  days: OrganizeDays;
  labelCodes: string[];
  entryIds: number[];
  allowedActions: string[];
}

export const DEFAULT_ORGANIZE_SCOPE: OrganizeScopeState = {
  days: 30,
  labelCodes: [],
  entryIds: [],
  allowedActions: ACTION_OPTIONS.map((a) => a.value),
};

function entryTitle(entry: EntryWithChildrenResponse): string {
  const title = parseContent(entry.content).title.trim();
  if (title) return title;
  const firstLine = entry.content.split('\n')[0]?.trim();
  return firstLine || '无标题记录';
}

function sourceKindLabel(kind: string): string {
  switch (kind) {
    case 'entry':
      return '记录';
    case 'attachment':
    case 'attachment_chunk':
      return '附件';
    default:
      return kind || '来源';
  }
}

interface EntryPickerProps {
  selectedIds: number[];
  labels: LabelResponse[];
  onChange: (entryIds: number[]) => void;
  compact?: boolean;
}

function EntryPicker({ selectedIds, labels, onChange, compact }: EntryPickerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const reqSeqRef = useRef(0);
  const [entries, setEntries] = useState<EntryWithChildrenResponse[]>([]);
  const [selectedCache, setSelectedCache] = useState<Map<number, EntryWithChildrenResponse>>(
    () => new Map(),
  );
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [open, setOpen] = useState(false);

  const labelByCode = useMemo(() => {
    const map = new Map<string, LabelResponse>();
    labels.forEach((label) => map.set(label.code, label));
    return map;
  }, [labels]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      setDebouncedSearch(search.trim());
    }, 250);
    return () => window.clearTimeout(handle);
  }, [search]);

  const loadPage = useCallback(async (offset: number, append: boolean, q: string) => {
    const seq = ++reqSeqRef.current;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setLoadError('');
    try {
      const data = await getEntries({
        limit: ENTRY_PAGE_SIZE,
        offset,
        q: q || undefined,
      });
      if (seq !== reqSeqRef.current) return;
      setTotal(data.total ?? data.items.length);
      setEntries((prev) => {
        if (!append) return data.items;
        const seen = new Set(prev.map((e) => e.id));
        const merged = [...prev];
        data.items.forEach((item) => {
          if (!seen.has(item.id)) merged.push(item);
        });
        return merged;
      });
      setSelectedCache((prev) => {
        const next = new Map(prev);
        data.items.forEach((item) => next.set(item.id, item));
        return next;
      });
    } catch {
      if (seq !== reqSeqRef.current) return;
      setLoadError('记录列表加载失败');
      if (!append) setEntries([]);
    } finally {
      if (seq === reqSeqRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadPage(0, false, debouncedSearch);
  }, [debouncedSearch, loadPage]);

  useEffect(() => {
    const onDocClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, []);

  const entryMap = useMemo(() => {
    const map = new Map<number, EntryWithChildrenResponse>(selectedCache);
    entries.forEach((entry) => map.set(entry.id, entry));
    return map;
  }, [entries, selectedCache]);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const toggleEntry = (entry: EntryWithChildrenResponse) => {
    setSelectedCache((prev) => {
      const next = new Map(prev);
      next.set(entry.id, entry);
      return next;
    });
    const set = new Set(selectedIds);
    if (set.has(entry.id)) set.delete(entry.id);
    else set.add(entry.id);
    onChange(Array.from(set));
  };

  const removeEntry = (entryId: number) => {
    onChange(selectedIds.filter((id) => id !== entryId));
  };

  const canLoadMore = entries.length < total;

  return (
    <div
      ref={rootRef}
      data-testid="organize-entry-picker"
      className={`growth-organize-entry-picker ${compact ? 'growth-organize-entry-picker--compact' : ''}`}
    >
      <div className="growth-organize-entry-picker-label">指定记录</div>
      {selectedIds.length > 0 && (
        <div className="growth-organize-entry-selected" aria-label="已选记录">
          {selectedIds.map((id) => {
            const entry = entryMap.get(id);
            const title = entry ? entryTitle(entry) : '已选记录';
            const labelText = entry
              ? entry.label_name || labelByCode.get(entry.label_code)?.name || entry.label_code
              : '';
            const dateText = entry?.created_at ? formatDate(entry.created_at) : '';
            return (
              <span key={id} className="growth-organize-entry-chip">
                <span className="growth-organize-entry-chip-main">
                  <span className="growth-organize-entry-chip-title">{title}</span>
                  {(dateText || labelText) && (
                    <span className="growth-organize-entry-chip-meta">
                      {[dateText, labelText].filter(Boolean).join(' · ')}
                    </span>
                  )}
                </span>
                <button
                  type="button"
                  className="growth-organize-entry-chip-remove"
                  aria-label={`移除 ${title}`}
                  onClick={() => removeEntry(id)}
                >
                  ×
                </button>
              </span>
            );
          })}
        </div>
      )}
      <div className="growth-organize-entry-search-wrap">
        <input
          data-testid="organize-entry-search"
          className="growth-organize-entry-search"
          type="search"
          value={search}
          placeholder="搜索标题、正文或标签…"
          aria-expanded={open}
          aria-controls="organize-entry-picker-list"
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setSearch(e.target.value);
            setOpen(true);
          }}
        />
        {open && (
          <div id="organize-entry-picker-list" className="growth-organize-entry-dropdown" role="listbox">
            {loading && entries.length === 0 && (
              <div className="growth-organize-entry-dropdown-empty">正在加载记录…</div>
            )}
            {loadError && (
              <div className="growth-organize-entry-dropdown-empty">{loadError}</div>
            )}
            {!loading && !loadError && entries.length === 0 && (
              <div className="growth-organize-entry-dropdown-empty">没有匹配的记录</div>
            )}
            {entries.map((entry) => {
              const checked = selectedSet.has(entry.id);
              const title = entryTitle(entry);
              const labelText = entry.label_name || labelByCode.get(entry.label_code)?.name || entry.label_code;
              return (
                <button
                  key={entry.id}
                  type="button"
                  role="option"
                  aria-selected={checked}
                  className={`growth-organize-entry-option ${checked ? 'is-selected' : ''}`}
                  onClick={() => toggleEntry(entry)}
                >
                  <span className="growth-organize-entry-option-check" aria-hidden="true">
                    {checked ? '✓' : ''}
                  </span>
                  <span className="growth-organize-entry-option-body">
                    <span className="growth-organize-entry-option-title">{title}</span>
                    <span className="growth-organize-entry-option-meta">
                      {formatDate(entry.created_at)} · {labelText}
                    </span>
                  </span>
                </button>
              );
            })}
            {canLoadMore && (
              <button
                type="button"
                className="growth-organize-entry-load-more"
                disabled={loadingMore}
                onClick={() => void loadPage(entries.length, true, debouncedSearch)}
              >
                {loadingMore ? '加载中…' : `加载更多（${entries.length}/${total}）`}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface OrganizeScopeBarProps {
  value: OrganizeScopeState;
  onChange: (next: OrganizeScopeState) => void;
  compact?: boolean;
  stageLabel?: string | null;
}

export function OrganizeScopeBar({ value, onChange, compact, stageLabel }: OrganizeScopeBarProps) {
  const [labels, setLabels] = useState<LabelResponse[]>([]);
  const [scope, setScope] = useState<OrganizeScopePreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    getLabels()
      .then((rows) => {
        if (!cancelled) setLabels(rows || []);
      })
      .catch(() => {
        if (!cancelled) setLabels([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await previewOrganizeScope({
        days: value.days,
        label_codes: value.labelCodes,
        entry_ids: value.entryIds.length > 0 ? value.entryIds : null,
      });
      setScope(data);
    } catch (err: unknown) {
      setScope(null);
      const code = err && typeof err === 'object' && 'code' in err
        ? String((err as { code?: string }).code || '')
        : '';
      setError(organizeErrorMessage(code, err instanceof Error ? err.message : '范围预览失败'));
    } finally {
      setLoading(false);
    }
  }, [value.days, value.labelCodes, value.entryIds]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggleLabel = (code: string) => {
    const set = new Set(value.labelCodes);
    if (set.has(code)) set.delete(code);
    else set.add(code);
    onChange({ ...value, labelCodes: Array.from(set) });
  };

  const toggleAction = (action: string) => {
    const set = new Set(value.allowedActions);
    if (set.has(action)) set.delete(action);
    else set.add(action);
    const next = Array.from(set);
    onChange({
      ...value,
      allowedActions: next.length > 0 ? next : ACTION_OPTIONS.map((a) => a.value),
    });
  };

  const metaText = (() => {
    if (stageLabel) return stageText(stageLabel);
    if (loading) return '正在计算范围…';
    if (error) return error;
    if (!scope) return '将整合 0 条记录';
    const matched = scope.matched_count ?? 0;
    const selected = scope.selected_count ?? 0;
    const trunc = scope.truncated ? '（已截断）' : '';
    return `匹配 ${matched} 条，纳入 ${selected} 条${trunc}`;
  })();

  return (
    <div className={`growth-organize-scope ${compact ? 'growth-organize-scope--compact' : ''}`}>
      <div className="growth-organize-scope-days" role="group" aria-label="时间范围">
        {DAY_OPTIONS.map((opt) => (
          <button
            key={String(opt.value)}
            type="button"
            className={`growth-organize-chip ${value.days === opt.value ? 'is-active' : ''}`}
            onClick={() => onChange({ ...value, days: opt.value })}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <div className="growth-organize-scope-labels" role="group" aria-label="标签筛选">
        <button
          type="button"
          className={`growth-organize-chip ${value.labelCodes.length === 0 ? 'is-active' : ''}`}
          onClick={() => onChange({ ...value, labelCodes: [] })}
        >
          全部标签
        </button>
        {labels.map((label) => (
          <button
            key={label.code}
            type="button"
            className={`growth-organize-chip ${value.labelCodes.includes(label.code) ? 'is-active' : ''}`}
            onClick={() => toggleLabel(label.code)}
          >
            {label.name || label.code}
          </button>
        ))}
      </div>
      <div className="growth-organize-scope-actions" role="group" aria-label="输出类型">
        {ACTION_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            className={`growth-organize-chip ${value.allowedActions.includes(opt.value) ? 'is-active' : ''}`}
            onClick={() => toggleAction(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <EntryPicker
        selectedIds={value.entryIds}
        labels={labels}
        compact={compact}
        onChange={(entryIds) => onChange({ ...value, entryIds })}
      />
      <div className="growth-organize-scope-meta" aria-live="polite">
        {metaText}
      </div>
    </div>
  );
}

function stageText(stage: string): string {
  switch (stage) {
    case 'scope':
      return '正在读取范围…';
    case 'extract':
      return '正在提取主题…';
    case 'summarize':
      return '正在生成草稿…';
    case 'validate':
      return '正在校验结果…';
    default:
      return '整理处理中…';
  }
}

interface DraftItem extends OrganizeDiffItem {
  _key: string;
  title: string;
  content: string;
}

interface OrganizeDiffPanelProps {
  preview: OrganizePreviewResponse;
  onSaved?: (count: number) => void;
  onDismiss?: () => void;
  onPreviewSource?: (refToken: string, previewSessionId?: string) => void;
}

function formatSourceDate(value: string): string {
  if (!value) return '';
  try {
    return formatDate(value);
  } catch {
    return value;
  }
}

function sourceMetaLine(source: OrganizeSourceDisplay): string {
  const parts = [formatSourceDate(source.created_at), sourceKindLabel(source.kind)];
  if (source.label_code) parts.push(source.label_code);
  return parts.filter(Boolean).join(' · ');
}

export function OrganizeDiffPanel({ preview, onSaved, onDismiss, onPreviewSource }: OrganizeDiffPanelProps) {
  const initialItems = useMemo(() => {
    return (preview.diff_preview || []).map((item, idx) => {
      const key = item.item_id || `idx-${idx}`;
      return {
        ...item,
        _key: key,
        title: String(item.title || item.action || ''),
        content: String(item.content || item.after || ''),
      } as DraftItem;
    });
  }, [preview.diff_preview]);

  const [drafts, setDrafts] = useState<DraftItem[]>(initialItems);
  const [selected, setSelected] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    initialItems.forEach((item) => {
      init[item._key] = true;
    });
    return init;
  });
  const [expandedSources, setExpandedSources] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    setDrafts(initialItems);
    const init: Record<string, boolean> = {};
    initialItems.forEach((item) => {
      init[item._key] = true;
    });
    setSelected(init);
    setExpandedSources({});
    setError('');
    setNotice('');
  }, [initialItems]);

  const groups = useMemo(() => {
    const order = ACTION_OPTIONS.map((a) => a.value);
    const map = new Map<string, DraftItem[]>();
    drafts.forEach((item) => {
      const action = item.action || 'create_summary';
      const list = map.get(action) || [];
      list.push(item);
      map.set(action, list);
    });
    return order
      .filter((a) => map.has(a))
      .map((a) => ({ action: a, label: ACTION_GROUP_LABEL[a] || a, items: map.get(a) || [] }));
  }, [drafts]);

  const selectedCount = drafts.filter((d) => selected[d._key]).length;

  const updateDraft = (key: string, patch: Partial<DraftItem>) => {
    setDrafts((prev) => prev.map((d) => (d._key === key ? { ...d, ...patch } : d)));
  };

  const handleSave = async () => {
    if (saving || selectedCount === 0) return;
    if (!preview.preview_token) {
      setError('缺少预览凭证，请重新生成整合建议');
      return;
    }
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const chosen: OrganizeDiffItem[] = drafts
        .filter((d) => selected[d._key])
        .map((d) => ({
          ...d,
          title: d.title,
          content: d.content,
          after: d.content,
        }));
      const result = await confirmOrganize({
        items: chosen,
        preview_token: preview.preview_token,
        preview_id: preview.preview_id,
        goal: preview.goal || '整理记录',
      });
      const count = result.created?.length || 0;
      setNotice(`已保存 ${count} 条整合结果`);
      onSaved?.(count);
    } catch (err: unknown) {
      const code = err && typeof err === 'object' && 'code' in err
        ? String((err as { code?: string }).code || '')
        : '';
      setError(organizeErrorMessage(code, err instanceof Error ? err.message : '保存失败，请重试'));
    } finally {
      setSaving(false);
    }
  };

  const previewSessionId = preview.preview_session_id ?? undefined;

  if (!drafts.length) {
    return (
      <div className="growth-organize-diff" data-testid="organize-diff-panel">
        <div className="growth-organize-diff-empty">当前范围暂无整合建议</div>
        {preview.scope && (
          <div className="growth-organize-scope-meta">
            匹配 {preview.scope.matched_count ?? 0} 条，纳入 {preview.scope.selected_count ?? 0} 条
            {preview.scope.truncated ? '（已截断）' : ''}
          </div>
        )}
        {onDismiss && (
          <button type="button" className="growth-organize-btn" onClick={onDismiss}>
            关闭
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="growth-organize-diff" data-testid="organize-diff-panel">
      {preview.scope && (
        <div className="growth-organize-scope-meta">
          匹配 {preview.scope.matched_count ?? 0} 条，纳入 {preview.scope.selected_count ?? 0} 条
          {preview.scope.truncated ? '（已截断）' : ''}
        </div>
      )}
      <div className="growth-organize-diff-toolbar">
        <button
          type="button"
          className="growth-organize-btn"
          onClick={() => {
            const next: Record<string, boolean> = {};
            drafts.forEach((d) => {
              next[d._key] = true;
            });
            setSelected(next);
          }}
        >
          全选
        </button>
        <button
          type="button"
          className="growth-organize-btn"
          onClick={() => {
            const next: Record<string, boolean> = {};
            drafts.forEach((d) => {
              next[d._key] = false;
            });
            setSelected(next);
          }}
        >
          取消全选
        </button>
        <span className="growth-organize-diff-count">
          已选 {selectedCount}/{drafts.length}
        </span>
      </div>
      {groups.map((group) => (
        <section key={group.action} className="growth-organize-diff-group">
          <h4 className="growth-organize-diff-group-title">{group.label}</h4>
          <ul className="growth-organize-diff-list">
            {group.items.map((item) => {
              const key = item._key;
              const sources = item.sources_display || [];
              const showSources = Boolean(expandedSources[key]);
              return (
                <li key={key} className="growth-organize-diff-item">
                  <label className="growth-organize-diff-check">
                    <input
                      type="checkbox"
                      checked={Boolean(selected[key])}
                      onChange={() =>
                        setSelected((prev) => ({ ...prev, [key]: !prev[key] }))
                      }
                    />
                    <span className="growth-organize-diff-title-label">标题</span>
                  </label>
                  <input
                    className="growth-organize-diff-title-input"
                    type="text"
                    value={item.title}
                    onChange={(e) => updateDraft(key, { title: e.target.value })}
                    aria-label="建议标题"
                  />
                  <textarea
                    className="growth-organize-diff-body-input"
                    value={item.content}
                    rows={4}
                    onChange={(e) => updateDraft(key, { content: e.target.value })}
                    aria-label="建议正文"
                  />
                  <div className="growth-organize-diff-meta">
                    <span>理由：{item.reason || '—'}</span>
                    <span>风险：{item.risk || 'medium'}</span>
                    <button
                      type="button"
                      className="growth-organize-link"
                      onClick={() =>
                        setExpandedSources((prev) => ({ ...prev, [key]: !prev[key] }))
                      }
                    >
                      {showSources ? '收起来源' : `查看来源（${sources.length}）`}
                    </button>
                  </div>
                  {showSources && (
                    <div className="growth-organize-diff-sources">
                      {sources.length === 0 ? (
                        <span className="growth-organize-diff-sources-empty">无来源</span>
                      ) : (
                        sources.map((source, sourceIdx) => {
                          const clickable = Boolean(source.ref_token && onPreviewSource);
                          const meta = sourceMetaLine(source);
                          return (
                            <button
                              key={`${source.title}-${source.created_at}-${sourceIdx}`}
                              type="button"
                              data-testid="organize-source-card"
                              className={`growth-organize-source-card ${clickable ? 'is-clickable' : ''}`}
                              disabled={!clickable}
                              onClick={() => {
                                if (source.ref_token && onPreviewSource) {
                                  onPreviewSource(source.ref_token, previewSessionId);
                                }
                              }}
                            >
                              <span className="growth-organize-source-card-title">{source.title || '参考记录'}</span>
                              <span className="growth-organize-source-card-meta">{meta}</span>
                            </button>
                          );
                        })
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      <div className="growth-organize-diff-actions">
        <button
          type="button"
          className="growth-organize-btn growth-organize-btn--primary"
          disabled={saving || selectedCount === 0 || !preview.preview_token}
          onClick={() => void handleSave()}
        >
          {saving ? '保存中…' : '保存所选'}
        </button>
        {onDismiss && (
          <button type="button" className="growth-organize-btn" disabled={saving} onClick={onDismiss}>
            放弃
          </button>
        )}
      </div>
      {error && <div className="growth-ai-error">{error}</div>}
      {notice && <div className="growth-ai-notice">{notice}</div>}
    </div>
  );
}
