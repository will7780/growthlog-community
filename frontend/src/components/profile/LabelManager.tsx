import { useState, useEffect, useCallback } from 'react';
import { Pencil, Trash2, Plus } from 'lucide-react';
import { UserLabel, profileApi } from '../../api/profile';
import { LabelEditDialog } from './LabelEditDialog';
import { DeleteLabelDialog } from './DeleteLabelDialog';
import IconButton from '../common/IconButton';

interface LabelManagerProps {
  onError?: (message: string) => void;
  onSuccess?: (message: string) => void;
}

export const LabelManager = ({ onError, onSuccess }: LabelManagerProps) => {
  const [labels, setLabels] = useState<UserLabel[]>([]);
  const [loading, setLoading] = useState(true);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editMode, setEditMode] = useState<'create' | 'edit'>('create');
  const [editingLabel, setEditingLabel] = useState<UserLabel | null>(null);
  const [editLoading, setEditLoading] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deletingLabel, setDeletingLabel] = useState<UserLabel | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);

  const loadLabels = useCallback(async () => {
    try {
      setLoading(true);
      const res = await profileApi.getUserLabels();
      setLabels(res.labels);
    } catch {
      onError?.('加载标签失败');
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    loadLabels();
  }, [loadLabels]);

  const handleCreate = () => {
    setEditMode('create');
    setEditingLabel(null);
    setEditDialogOpen(true);
  };

  const handleEdit = (label: UserLabel) => {
    setEditMode('edit');
    setEditingLabel(label);
    setEditDialogOpen(true);
  };

  const handleDelete = (label: UserLabel) => {
    setDeletingLabel(label);
    setDeleteDialogOpen(true);
  };

  const handleEditSubmit = async (name: string) => {
    try {
      setEditLoading(true);
      if (editMode === 'create') {
        await profileApi.createLabel({ name });
        onSuccess?.('标签创建成功');
      } else if (editingLabel) {
        await profileApi.updateLabel(editingLabel.code, { name });
        onSuccess?.('标签更新成功');
      }
      setEditDialogOpen(false);
      loadLabels();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        '操作失败';
      onError?.(typeof msg === 'string' ? msg : '操作失败');
    } finally {
      setEditLoading(false);
    }
  };

  const handleDeleteConfirm = async (targetCode?: string) => {
    if (!deletingLabel) return;
    try {
      setDeleteLoading(true);
      await profileApi.deleteLabel(deletingLabel.code, targetCode);
      onSuccess?.('标签删除成功');
      setDeleteDialogOpen(false);
      setDeletingLabel(null);
      loadLabels();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        '删除失败';
      onError?.(typeof msg === 'string' ? msg : '删除失败');
    } finally {
      setDeleteLoading(false);
    }
  };

  if (loading) {
    return <div className="growth-state">标签加载中...</div>;
  }

  return (
    <div className="label-manager">
      <div className="label-manager__head">
        <div>
          <h3 className="label-manager__title">标签管理</h3>
          <p className="label-manager__subtitle">共 {labels.length} 个标签（最少保留 2 个）</p>
        </div>
        <button
          type="button"
          onClick={handleCreate}
          disabled={labels.length >= 7}
          className="entry-sheet-btn entry-sheet-btn--primary label-manager__create"
          title="新建标签"
          aria-label="新建标签"
        >
          <Plus size={16} aria-hidden="true" />
          <span>新建</span>
        </button>
      </div>

      {labels.length === 0 ? (
        <div className="growth-state growth-state--empty">暂无标签</div>
      ) : (
        labels.map((label) => (
          <div key={label.code} className="label-manager__row">
            <div className="label-manager__meta">
              <span className="label-manager__name" title={label.name}>
                {label.name}
              </span>
              {label.is_system && <span className="growth-attachment-status growth-attachment-status--uploaded">系统</span>}
              <span className="growth-muted-text">{label.entry_count} 条</span>
            </div>
            <div className="label-manager__actions">
              <IconButton
                icon={Pencil}
                label={label.is_system ? '系统标签不可修改' : '编辑标签'}
                size="sm"
                variant="ghost"
                onClick={() => handleEdit(label)}
                disabled={label.is_system}
              />
              <IconButton
                icon={Trash2}
                label={!label.can_delete ? '系统标签不可删除' : '删除标签'}
                size="sm"
                variant="ghost"
                onClick={() => handleDelete(label)}
                disabled={!label.can_delete}
              />
            </div>
          </div>
        ))
      )}

      <LabelEditDialog
        open={editDialogOpen}
        onClose={() => setEditDialogOpen(false)}
        onSubmit={handleEditSubmit}
        initialName={editingLabel?.name || ''}
        mode={editMode}
        loading={editLoading}
      />

      <DeleteLabelDialog
        open={deleteDialogOpen}
        onClose={() => setDeleteDialogOpen(false)}
        onConfirm={handleDeleteConfirm}
        label={deletingLabel}
        availableTargetLabels={labels}
        loading={deleteLoading}
      />
    </div>
  );
};
