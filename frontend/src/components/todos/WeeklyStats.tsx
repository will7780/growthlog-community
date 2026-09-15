/**
 * 本周统计组件
 * 显示本周小要事的完成情况
 */
import { useTodos } from '../../contexts/TodoContext';

export default function WeeklyStats() {
  const { weeklyStats } = useTodos();

  if (!weeklyStats) {
    return null;
  }

  const { total, completed, completion_rate } = weeklyStats;

  return (
    <div className="weekly-stats">
      <span className="weekly-stats__label">本周完成:</span>
      <span className="weekly-stats__value">
        {completed}/{total} ({completion_rate}%)
      </span>
    </div>
  );
}
