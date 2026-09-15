/**
 * 标签筛选组件
 */
import type { LabelResponse } from '../../types/api';

interface LabelFilterProps {
  labels: LabelResponse[];
  selectedCode: string | null;
  onSelect: (code: string | null) => void;
}

export default function LabelFilter({ labels, selectedCode, onSelect }: LabelFilterProps) {
  return (
    <div className="growth-label-filter">
      <div className="growth-label-scroll">
        <button
          type="button"
          onClick={() => onSelect(null)}
          className={`growth-label-chip ${
            selectedCode === null
              ? 'growth-label-chip--active'
              : ''
          }`}
        >
          全部
        </button>
        {labels.map((label) => (
          <button
            key={label.code}
            type="button"
            onClick={() => onSelect(label.code)}
            className={`growth-label-chip ${
              selectedCode === label.code
                ? 'growth-label-chip--active'
                : ''
            }`}
          >
            {label.name}
          </button>
        ))}
      </div>
    </div>
  );
}
