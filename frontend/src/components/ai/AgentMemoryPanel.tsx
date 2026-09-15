import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  analyzeAgentMemory,
  createAgentMemory,
  deleteAgentMemory,
  getAgentMemories,
  mergeAgentMemories,
  updateAgentMemory,
} from '../../api/ai';
import type { AgentMemoryItem } from '../../types/api';

const MEMORY_TYPES: AgentMemoryItem['memory_type'][] = [
  'goal',
  'preference',
  'project',
  'profile',
  'insight',
];

const MEMORY_TYPE_LABELS: Record<AgentMemoryItem['memory_type'], string> = {
  goal: '长期目标',
  preference: '表达偏好',
  project: '项目背景',
  profile: '使用特征',
  insight: '长期关注',
};

const DISPLAY_LIMIT = 6;

const buildMergedMemoryContent = (items: AgentMemoryItem[]) =>
  items
    .map((item) => item.content.trim())
    .filter(Boolean)
    .join('\n');

interface AgentMemoryPanelProps {
  open: boolean;
  onClose: () => void;
  variant?: 'desktop' | 'mobile';
}

export default function AgentMemoryPanel({
  open,
  onClose,
  variant = 'desktop',
}: AgentMemoryPanelProps) {
  const isMobile = variant === 'mobile';
  const prefix = isMobile ? 'ai-mp-memory' : 'growth-ai-memory';

  const [memories, setMemories] = useState<AgentMemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [deactivatingId, setDeactivatingId] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [qualityWarnings, setQualityWarnings] = useState<Array<Record<string, unknown>>>([]);
  const [confirmWithWarnings, setConfirmWithWarnings] = useState(false);
  const [memoryType, setMemoryType] = useState<AgentMemoryItem['memory_type']>('insight');
  const [content, setContent] = useState('');

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [mergeTargetId, setMergeTargetId] = useState<number | null>(null);
  const [mergeContent, setMergeContent] = useState('');
  const [mergeConfirming, setMergeConfirming] = useState(false);
  const [mergeError, setMergeError] = useState('');
  const [merging, setMerging] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editType, setEditType] = useState<AgentMemoryItem['memory_type']>('insight');
  const [editContent, setEditContent] = useState('');
  const [editWarnings, setEditWarnings] = useState<Array<Record<string, unknown>>>([]);
  const [editConfirmWithWarnings, setEditConfirmWithWarnings] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editAnalyzing, setEditAnalyzing] = useState(false);

  const loadMemories = useCallback(async (highlight?: AgentMemoryItem) => {
    setLoading(true);
    setError('');
    try {
      const rows = await getAgentMemories();
      const limited = rows.slice(0, DISPLAY_LIMIT);
      setMemories(
        highlight
          ? limited.map((memory) => (memory.id === highlight.id ? { ...memory, quality_warnings: highlight.quality_warnings } : memory))
          : limited,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载个性设置失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setQualityWarnings([]);
    setConfirmWithWarnings(false);
  }, [memoryType, content]);

  useEffect(() => {
    setEditWarnings([]);
    setEditConfirmWithWarnings(false);
  }, [editType, editContent, editingId]);

  const selectedMemories = useMemo(
    () => memories.filter((memory) => selectedIds.includes(memory.id) && memory.is_active !== false),
    [memories, selectedIds],
  );
  const selectedIdsKey = useMemo(
    () => selectedIds.slice().sort((a, b) => a - b).join(','),
    [selectedIds],
  );
  const selectedTypeCount = new Set(selectedMemories.map((memory) => memory.memory_type)).size;
  const mergeBlockedReason =
    selectedMemories.length < 2
      ? '至少选择两条个性设置'
      : selectedTypeCount > 1
        ? '仅支持合并同类型个性设置'
        : '';
  const canSubmitMerge = !mergeBlockedReason && mergeTargetId != null && Boolean(mergeContent.trim()) && !merging;

  const resetMergeState = useCallback(() => {
    setSelectedIds([]);
    setMergeTargetId(null);
    setMergeContent('');
    setMergeConfirming(false);
    setMergeError('');
    setMerging(false);
  }, []);

  useEffect(() => {
    if (!open) {
      resetMergeState();
      setEditingId(null);
      setEditWarnings([]);
      setEditConfirmWithWarnings(false);
      return;
    }
    loadMemories();
  }, [open, loadMemories, resetMergeState]);

  useEffect(() => {
    setSelectedIds((current) => {
      const activeIds = new Set(
        memories.filter((memory) => memory.is_active !== false).map((memory) => memory.id),
      );
      const next = current.filter((id) => activeIds.has(id));
      return next.length === current.length ? current : next;
    });
  }, [memories]);

  useEffect(() => {
    if (selectedMemories.length === 0) {
      setMergeTargetId(null);
      setMergeContent('');
      setMergeConfirming(false);
      setMergeError('');
      return;
    }

    setMergeTargetId((current) => (
      current != null && selectedIds.includes(current) ? current : selectedMemories[0].id
    ));
    // Rebuild draft when selection set changes; keep user edits until selection changes.
    setMergeContent(buildMergedMemoryContent(selectedMemories));
    setMergeConfirming(false);
  }, [selectedIdsKey]); // eslint-disable-line react-hooks/exhaustive-deps -- sync on selection set only

  const beginEdit = (memory: AgentMemoryItem) => {
    setEditingId(memory.id);
    setEditType(memory.memory_type);
    setEditContent(memory.content || '');
    setEditWarnings([]);
    setEditConfirmWithWarnings(false);
    setError('');
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditContent('');
    setEditWarnings([]);
    setEditConfirmWithWarnings(false);
    setEditSaving(false);
    setEditAnalyzing(false);
  };

  const toggleMemorySelection = (memoryId: number) => {
    setMergeConfirming(false);
    setMergeError('');
    setSelectedIds((current) =>
      current.includes(memoryId) ? current.filter((id) => id !== memoryId) : [...current, memoryId],
    );
  };

  const runQualityCheck = async () => {
    const trimmed = content.trim();
    if (!trimmed || analyzing) return [];

    setAnalyzing(true);
    setError('');
    try {
      const result = await analyzeAgentMemory({
        memory_type: memoryType,
        content: trimmed,
      });
      setQualityWarnings(result.quality_warnings || []);
      return result.quality_warnings || [];
    } catch (err) {
      setError(err instanceof Error ? err.message : '检查个性设置失败');
      return [];
    } finally {
      setAnalyzing(false);
    }
  };

  const runEditQualityCheck = async (memoryId: number) => {
    const trimmed = editContent.trim();
    if (!trimmed || editAnalyzing) return [];

    setEditAnalyzing(true);
    setError('');
    try {
      const result = await analyzeAgentMemory({
        memory_type: editType,
        content: trimmed,
        exclude_memory_id: memoryId,
      });
      setEditWarnings(result.quality_warnings || []);
      return result.quality_warnings || [];
    } catch (err) {
      setError(err instanceof Error ? err.message : '检查个性设置失败');
      return [];
    } finally {
      setEditAnalyzing(false);
    }
  };

  const handleCreate = async () => {
    const trimmed = content.trim();
    if (!trimmed || saving) return;

    setSaving(true);
    setError('');
    try {
      if (!confirmWithWarnings) {
        const warnings = await runQualityCheck();
        if (warnings.length > 0) {
          setConfirmWithWarnings(true);
          return;
        }
      }

      const created = await createAgentMemory({
        memory_type: memoryType,
        content: trimmed,
        source: 'user',
      });
      setContent('');
      setQualityWarnings(created.quality_warnings || []);
      setConfirmWithWarnings(false);
      await loadMemories(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存个性设置失败');
    } finally {
      setSaving(false);
    }
  };

  const handleUpdate = async (memoryId: number) => {
    const trimmed = editContent.trim();
    if (!trimmed || editSaving) return;

    setEditSaving(true);
    setError('');
    try {
      if (!editConfirmWithWarnings) {
        const warnings = await runEditQualityCheck(memoryId);
        if (warnings.length > 0) {
          setEditConfirmWithWarnings(true);
          return;
        }
      }

      const updated = await updateAgentMemory(memoryId, {
        memory_type: editType,
        content: trimmed,
      });
      cancelEdit();
      await loadMemories(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新个性设置失败');
    } finally {
      setEditSaving(false);
    }
  };

  const renderWarnings = (warnings?: Array<Record<string, unknown>>) => {
    if (!warnings || warnings.length === 0) return null;
    return (
      <div className={`${prefix}-warnings`}>
        {warnings.slice(0, 3).map((warning, index) => (
          <div key={`${String(warning.kind || 'warning')}-${index}`} className={`${prefix}-warning`}>
            <span>{String(warning.kind || '') === 'possible_conflict' ? '可能冲突' : '可能重复'}</span>
            <p>{String(warning.message || '建议人工确认后再保存。')}</p>
          </div>
        ))}
      </div>
    );
  };

  const handleDeactivate = async (memoryId: number) => {
    if (deactivatingId != null) return;

    setDeactivatingId(memoryId);
    setError('');
    try {
      await deleteAgentMemory(memoryId);
      if (editingId === memoryId) {
        cancelEdit();
      }
      await loadMemories();
    } catch (err) {
      setError(err instanceof Error ? err.message : '停用个性设置失败');
    } finally {
      setDeactivatingId(null);
    }
  };

  const handleMerge = async () => {
    if (merging) return;

    if (mergeBlockedReason || mergeTargetId == null) {
      setMergeError(mergeBlockedReason || '请选择保留目标');
      return;
    }

    const sourceMemoryIds = selectedIds.filter((id) => id !== mergeTargetId);
    if (sourceMemoryIds.length === 0) {
      setMergeError('至少选择一条待合并来源个性设置');
      return;
    }

    const trimmed = mergeContent.trim();
    if (!trimmed) {
      setMergeError('合并内容不能为空');
      return;
    }

    if (!mergeConfirming) {
      setMergeConfirming(true);
      setMergeError('');
      return;
    }

    setMerging(true);
    setMergeError('');
    try {
      const result = await mergeAgentMemories(mergeTargetId, {
        source_memory_ids: sourceMemoryIds,
        content: trimmed,
      });
      resetMergeState();
      await loadMemories(result.memory);
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : '合并个性设置失败');
    } finally {
      setMerging(false);
    }
  };

  if (!open) return null;

  return (
    <div className={`${prefix}-backdrop`} role="presentation" onClick={onClose}>
      <div
        className={`${prefix}-panel`}
        role="dialog"
        aria-modal="true"
        aria-label="个性设置"
        onClick={(event) => event.stopPropagation()}
      >
        <div className={`${prefix}-header`}>
          <div className={`${prefix}-title`}>个性设置</div>
          <div className={`${prefix}-header-actions`}>
            <button type="button" className={`${prefix}-refresh`} onClick={() => loadMemories()} disabled={loading}>
              刷新
            </button>
            <button type="button" className={`${prefix}-close`} onClick={onClose}>
              关闭
            </button>
          </div>
        </div>

        <div className={`${prefix}-body`}>
          {error && <div className={`${prefix}-error`}>{error}</div>}
          <div className={`${prefix}-note`}>
            个性设置会影响助手的表达方式和背景理解，不会替代原记录，也不会改变引用来源。
          </div>

          <div className={`${prefix}-list`}>
            {loading && memories.length === 0 && (
              <div className={`${prefix}-empty`}>加载中…</div>
            )}
            {!loading && memories.length === 0 && (
              <div className={`${prefix}-empty`}>暂无启用个性设置</div>
            )}
            {memories.map((memory) => {
              const isEditing = editingId === memory.id;
              return (
                <div key={memory.id} className={`${prefix}-item${isEditing ? ` ${prefix}-item-editing` : ''}`}>
                  <div className={`${prefix}-item-head`}>
                    <div className={`${prefix}-item-title`}>
                      {!isEditing && (
                        <label className={`${prefix}-select-memory`}>
                          <input
                            type="checkbox"
                            checked={selectedIds.includes(memory.id)}
                            onChange={() => toggleMemorySelection(memory.id)}
                            disabled={merging || deactivatingId != null}
                            aria-label={`选择个性设置 ${memory.id}`}
                          />
                          <span>选择</span>
                        </label>
                      )}
                      {!isEditing && (
                        <span className={`${prefix}-type`}>
                          {MEMORY_TYPE_LABELS[memory.memory_type] || memory.memory_type}
                        </span>
                      )}
                      {isEditing && <span className={`${prefix}-type`}>编辑中</span>}
                    </div>
                    <div className={`${prefix}-item-actions`}>
                      {!isEditing && (
                        <button
                          type="button"
                          className={`${prefix}-edit`}
                          onClick={() => beginEdit(memory)}
                          disabled={editSaving || deactivatingId != null}
                        >
                          编辑
                        </button>
                      )}
                      <button
                        type="button"
                        className={`${prefix}-deactivate`}
                        onClick={() => handleDeactivate(memory.id)}
                        disabled={deactivatingId === memory.id || (isEditing && editSaving)}
                      >
                        {deactivatingId === memory.id ? '停用中…' : '停用'}
                      </button>
                    </div>
                  </div>

                  {isEditing ? (
                    <div className={`${prefix}-edit-form`}>
                      <label className={`${prefix}-label`} htmlFor={`${prefix}-edit-type-${memory.id}`}>
                        类型
                      </label>
                      <select
                        id={`${prefix}-edit-type-${memory.id}`}
                        className={`${prefix}-select`}
                        value={editType}
                        onChange={(event) => setEditType(event.target.value as AgentMemoryItem['memory_type'])}
                        disabled={editSaving || editAnalyzing}
                      >
                        {MEMORY_TYPES.map((type) => (
                          <option key={type} value={type}>
                            {MEMORY_TYPE_LABELS[type]}
                          </option>
                        ))}
                      </select>
                      <label className={`${prefix}-label`} htmlFor={`${prefix}-edit-content-${memory.id}`}>
                        内容
                      </label>
                      <textarea
                        id={`${prefix}-edit-content-${memory.id}`}
                        className={`${prefix}-textarea`}
                        value={editContent}
                        onChange={(event) => setEditContent(event.target.value)}
                        rows={3}
                        maxLength={2000}
                        disabled={editSaving || editAnalyzing}
                      />
                      {renderWarnings(editWarnings)}
                      <div className={`${prefix}-edit-actions`}>
                        <button
                          type="button"
                          className={`${prefix}-cancel`}
                          onClick={cancelEdit}
                          disabled={editSaving}
                        >
                          取消
                        </button>
                        <button
                          type="button"
                          className={`${prefix}-check`}
                          disabled={editAnalyzing || editSaving || !editContent.trim()}
                          onClick={() => runEditQualityCheck(memory.id)}
                        >
                          {editAnalyzing ? '检查中…' : '检查重复/冲突'}
                        </button>
                        <button
                          type="button"
                          className={`${prefix}-save`}
                          disabled={editSaving || editAnalyzing || !editContent.trim()}
                          onClick={() => handleUpdate(memory.id)}
                        >
                          {editSaving ? '保存中…' : editConfirmWithWarnings ? '仍然保存' : '保存'}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className={`${prefix}-content`}>{memory.content}</div>
                      <div className={`${prefix}-meta`}>
                        <span>来源 {memory.source || 'user'}</span>
                        <span>置信 {(memory.confidence ?? 0).toFixed(2)}</span>
                      </div>
                      {renderWarnings(memory.quality_warnings)}
                    </>
                  )}
                </div>
              );
            })}
          </div>

          {selectedIds.length > 0 && (
            <div className={`${prefix}-merge`}>
              <div className={`${prefix}-merge-head`}>
                <span className={`${prefix}-merge-count`}>{selectedMemories.length} 条已选</span>
                <button type="button" className={`${prefix}-cancel`} onClick={resetMergeState} disabled={merging}>
                  清空
                </button>
              </div>
              <label className={`${prefix}-label`} htmlFor={`${prefix}-merge-target`}>
                保留为
              </label>
              <select
                id={`${prefix}-merge-target`}
                className={`${prefix}-select`}
                value={mergeTargetId ?? ''}
                onChange={(event) => {
                  setMergeTargetId(Number(event.target.value));
                  setMergeConfirming(false);
                  setMergeError('');
                }}
                disabled={merging || selectedMemories.length === 0 || Boolean(mergeBlockedReason)}
              >
                {selectedMemories.map((memory) => (
                  <option key={memory.id} value={memory.id}>
                    {MEMORY_TYPE_LABELS[memory.memory_type]} · {(memory.content || '').slice(0, 18)}
                  </option>
                ))}
              </select>
              <label className={`${prefix}-label`} htmlFor={`${prefix}-merge-content`}>
                合并内容
              </label>
              <textarea
                id={`${prefix}-merge-content`}
                className={`${prefix}-textarea ${prefix}-merge-textarea`}
                value={mergeContent}
                onChange={(event) => {
                  setMergeContent(event.target.value);
                  setMergeConfirming(false);
                  setMergeError('');
                }}
                rows={3}
                maxLength={4000}
                disabled={merging || Boolean(mergeBlockedReason)}
              />
              {(mergeError || mergeBlockedReason) && (
                <div className={`${prefix}-error`}>{mergeError || mergeBlockedReason}</div>
              )}
              {mergeConfirming && !mergeBlockedReason && (
                <div className={`${prefix}-merge-hint`}>将软停用其他已选个性设置，并保留上方目标内容。请再次确认。</div>
              )}
              <button
                type="button"
                className={`${prefix}-save`}
                disabled={!canSubmitMerge}
                onClick={handleMerge}
              >
                {merging ? '合并中…' : mergeConfirming ? '确认合并' : '合并个性设置'}
              </button>
            </div>
          )}

          <form
            className={`${prefix}-form`}
            onSubmit={(event) => {
              event.preventDefault();
              handleCreate();
            }}
          >
            <label className={`${prefix}-label`} htmlFor={`${prefix}-type-select`}>
              类型
            </label>
            <select
              id={`${prefix}-type-select`}
              className={`${prefix}-select`}
              value={memoryType}
              onChange={(event) => setMemoryType(event.target.value as AgentMemoryItem['memory_type'])}
            >
              {MEMORY_TYPES.map((type) => (
                <option key={type} value={type}>
                  {MEMORY_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
            <label className={`${prefix}-label`} htmlFor={`${prefix}-content-input`}>
              内容
            </label>
            <textarea
              id={`${prefix}-content-input`}
              className={`${prefix}-textarea`}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder="例如：近期优先推进论文实验复现"
              rows={3}
              maxLength={2000}
            />
            {renderWarnings(qualityWarnings)}
            <div className={`${prefix}-form-actions`}>
              <button
                type="button"
                className={`${prefix}-check`}
                disabled={analyzing || saving || !content.trim()}
                onClick={runQualityCheck}
              >
                {analyzing ? '检查中…' : '检查重复/冲突'}
              </button>
              <button type="submit" className={`${prefix}-save`} disabled={saving || analyzing || !content.trim()}>
                {saving ? '保存中…' : confirmWithWarnings ? '仍然保存' : '保存个性设置'}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
