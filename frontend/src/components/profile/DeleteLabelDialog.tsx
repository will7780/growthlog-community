import React, { useState } from 'react';
import { UserLabel } from '../../api/profile';

interface DeleteLabelDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: (targetCode?: string) => void;
  label: UserLabel | null;
  availableTargetLabels: UserLabel[];
  loading?: boolean;
}

export const DeleteLabelDialog: React.FC<DeleteLabelDialogProps> = ({
  open,
  onClose,
  onConfirm,
  label,
  availableTargetLabels,
  loading = false,
}) => {
  const [selectedTarget, setSelectedTarget] = useState<string>('');

  if (!open || !label) return null;

  const hasEntries = label.entry_count > 0;

  const handleConfirm = () => {
    if (hasEntries && !selectedTarget) {
      return;
    }
    onConfirm(selectedTarget || undefined);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white rounded-xl shadow-2xl w-full max-w-md mx-4 overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100">
          <h3 className="text-lg font-semibold text-gray-800">删除标签</h3>
        </div>
        <div className="px-6 py-4">
          <p className="text-gray-600 mb-4">
            确定要删除标签「<span className="font-semibold text-gray-800">{label.name}</span>」吗？
          </p>

          {hasEntries ? (
            <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
              <p className="text-amber-800 text-sm mb-3">
                该标签下有 <span className="font-semibold">{label.entry_count}</span> 条记录，
                请选择归档目标：
              </p>
              <select
                value={selectedTarget}
                onChange={(e) => setSelectedTarget(e.target.value)}
                className="w-full px-4 py-2.5 border border-gray-200 rounded-lg focus:ring-2 focus:ring-red-500 focus:border-transparent outline-none bg-white"
                disabled={loading}
              >
                <option value="">请选择归档目标</option>
                {availableTargetLabels
                  .filter((l) => l.code !== label.code)
                  .map((l) => (
                    <option key={l.code} value={l.code}>
                      {l.name}（{l.entry_count} 条记录）
                    </option>
                  ))}
              </select>
            </div>
          ) : (
            <p className="text-gray-500 text-sm">该标签下暂无记录，可以直接删除。</p>
          )}
        </div>
        <div className="px-6 py-4 bg-gray-50 flex justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-gray-600 hover:text-gray-800 font-medium transition-colors"
            disabled={loading}
          >
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading || (hasEntries && !selectedTarget)}
            className="px-6 py-2 bg-red-500 text-white rounded-lg font-medium hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? '删除中...' : '确认删除'}
          </button>
        </div>
      </div>
    </div>
  );
};