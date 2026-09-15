import type {
  AgentAnalysis,
  AnalysisEvidenceEntry,
  AnalysisEvidenceItem,
  AnalysisModuleResult,
} from '../../types/api';

const MODULE_LABELS: Record<string, string> = {
  timeline: '时间线',
  weekly_review: '本周复盘',
  monthly_review: '本月复盘',
  goal_progress: '目标推进',
  emotion_pattern: '情绪线索',
  knowledge_clusters: '知识主题',
};

const MODULE_ORDER = [
  'timeline',
  'weekly_review',
  'monthly_review',
  'goal_progress',
  'emotion_pattern',
  'knowledge_clusters',
] as const;

const REASON_LABELS: Record<string, string> = {
  timeline_recent_entries: '近期记录',
  weekly_review_recent_entries: '本周记录',
  monthly_review_recent_entries: '本月记录',
  goal_progress_context_entries: '推进上下文',
  goal_related_entry: '目标相关记录',
  active_goal: '活跃目标',
  open_todo: '未完成待办',
};

function formatReason(reason?: string): string {
  if (!reason) return '';
  if (REASON_LABELS[reason]) return REASON_LABELS[reason];
  if (reason.startsWith('cluster:')) return `标签代表 · ${reason.slice(8)}`;
  if (reason.startsWith('emotion_match:')) return '情绪关键词匹配';
  return reason.replace(/_/g, ' ');
}

function itemTypeLabel(sourceType?: string): string {
  if (sourceType === 'memory') return '个性设置';
  if (sourceType === 'todo') return '小要事';
  return '线索';
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function moduleHasDetail(module?: AnalysisModuleResult): boolean {
  if (!module) return false;
  return Object.keys(module).some((key) => {
    const value = module[key];
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === 'number') return true;
    if (typeof value === 'string' && value.trim()) return true;
    return false;
  });
}

export function hasAnalysisDetailContent(analysis?: AgentAnalysis | null): boolean {
  if (!analysis) return false;
  return MODULE_ORDER.some((key) => moduleHasDetail(analysis[key]));
}

function buildMetrics(key: string, module: AnalysisModuleResult): string[] {
  const metrics: string[] = [];

  const entryCount = asNumber(module.entry_count);
  if (entryCount != null) metrics.push(`记录 ${entryCount} 条`);

  const windowDays = asNumber(module.window_days);
  if (windowDays != null) metrics.push(`近 ${windowDays} 天`);

  const days = asNumber(module.days);
  if (days != null && key !== 'timeline') metrics.push(`统计窗口 ${days} 天`);

  const openTodos = asNumber(module.open_todos);
  if (openTodos != null) metrics.push(`未完成待办 ${openTodos} 条`);

  const completedTodos = asNumber(module.completed_todos);
  if (completedTodos != null) metrics.push(`已完成待办 ${completedTodos} 条`);

  const todoTotal = asNumber(module.todo_total);
  if (todoTotal != null) metrics.push(`待办总计 ${todoTotal} 条`);

  const todoOpen = asNumber(module.todo_open);
  if (todoOpen != null) metrics.push(`未完成 ${todoOpen} 条`);

  const dailyCounts = asArray(module.daily_counts);
  if (dailyCounts.length > 0) {
    const total = dailyCounts.reduce((sum: number, item) => {
      if (item && typeof item === 'object' && 'count' in item) {
        return sum + (asNumber((item as { count?: number }).count) || 0);
      }
      return sum;
    }, 0);
    if (total > 0) metrics.push(`活跃日记录 ${total} 条`);
  }

  const labels = asArray(module.labels);
  if (labels.length > 0) metrics.push(`标签 ${labels.length} 类`);

  const goals = asArray(module.goals);
  if (goals.length > 0) metrics.push(`活跃目标 ${goals.length} 条`);

  const openTodoItems = asArray(module.open_todos);
  if (key === 'goal_progress' && openTodoItems.length > 0) {
    metrics.push(`关联待办 ${openTodoItems.length} 条`);
  }

  const matchedEntries = asArray(module.matched_entries);
  if (matchedEntries.length > 0) metrics.push(`命中线索 ${matchedEntries.length} 条`);

  const clusters = asArray(module.clusters);
  if (clusters.length > 0) {
    const clusterEntries = clusters.reduce((sum: number, item) => {
      if (item && typeof item === 'object' && 'entry_count' in item) {
        return sum + (asNumber((item as { entry_count?: number }).entry_count) || 0);
      }
      return sum;
    }, 0);
    metrics.push(`主题簇 ${clusters.length} 类`);
    if (clusterEntries > 0) metrics.push(`簇内记录 ${clusterEntries} 条`);
  }

  return metrics;
}

function collectModules(analysis: AgentAnalysis) {
  return MODULE_ORDER.map((key) => ({
    key,
    label: MODULE_LABELS[key] || key,
    module: analysis[key],
  })).filter(({ module }) => moduleHasDetail(module));
}

interface AnalysisDetailPanelProps {
  analysis?: AgentAnalysis | null;
  open: boolean;
  onClose: () => void;
  variant?: 'desktop' | 'mobile';
  onEntryClick?: (entryId: number) => void;
}

export default function AnalysisDetailPanel({
  analysis,
  open,
  onClose,
  variant = 'desktop',
  onEntryClick,
}: AnalysisDetailPanelProps) {
  if (!open || !analysis || !hasAnalysisDetailContent(analysis)) return null;

  const isMobile = variant === 'mobile';
  const prefix = isMobile ? 'ai-mp-analysis-detail' : 'growth-ai-analysis-detail';
  const modules = collectModules(analysis);

  return (
    <div
      className={`${prefix}-backdrop`}
      role="presentation"
      onClick={onClose}
    >
      <div
        className={`${prefix}-panel`}
        role="dialog"
        aria-modal="true"
        aria-label="分析详情"
        onClick={(event) => event.stopPropagation()}
      >
        <div className={`${prefix}-header`}>
          <div className={`${prefix}-title`}>分析详情</div>
          <button type="button" className={`${prefix}-close`} onClick={onClose}>
            关闭
          </button>
        </div>

        <div className={`${prefix}-body`}>
          {modules.map(({ key, label, module }) => {
            if (!module) return null;
            const metrics = buildMetrics(key, module);
            const factEntries = (module.evidence_entries || []).slice(0, 8);
            const clueItems = (module.evidence_items || []).slice(0, 8);
            const matchedEntries = asArray(module.matched_entries).slice(0, 8) as Array<{
              snippet?: string;
              matched_terms?: string[];
            }>;
            const hasFacts = factEntries.length > 0;
            const hasClues = clueItems.length > 0 || matchedEntries.length > 0 || (!hasFacts && metrics.length > 0);

            return (
              <section key={key} className={`${prefix}-module`}>
                <div className={`${prefix}-module-title`}>{label}</div>
                {metrics.length > 0 && (
                  <div className={`${prefix}-metrics`}>
                    {metrics.map((metric) => (
                      <span key={metric} className={`${prefix}-metric`}>
                        {metric}
                      </span>
                    ))}
                  </div>
                )}

                {hasFacts && (
                  <div className={`${prefix}-section`}>
                    <div className={`${prefix}-section-label`}>事实依据</div>
                    {factEntries.map((entry: AnalysisEvidenceEntry, idx) => {
                      const title = entry.title || '相关记录';
                      const snippet = entry.snippet || '';
                      const reason = formatReason(entry.reason);
                      const clickable = Boolean(entry.entry_id && onEntryClick);

                      if (clickable) {
                        return (
                          <button
                            key={`fact-${key}-${idx}`}
                            type="button"
                            className={`${prefix}-entry ${prefix}-entry--clickable`}
                            onClick={() => onEntryClick?.(entry.entry_id as number)}
                          >
                            <div className={`${prefix}-entry-title`}>{title}</div>
                            {snippet && <div className={`${prefix}-entry-snippet`}>{snippet}</div>}
                            {reason && <div className={`${prefix}-entry-reason`}>{reason}</div>}
                          </button>
                        );
                      }

                      return (
                        <div key={`fact-${key}-${idx}`} className={`${prefix}-entry`}>
                          <div className={`${prefix}-entry-title`}>{title}</div>
                          {snippet && <div className={`${prefix}-entry-snippet`}>{snippet}</div>}
                          {reason && <div className={`${prefix}-entry-reason`}>{reason}</div>}
                        </div>
                      );
                    })}
                  </div>
                )}

                {hasClues && (
                  <div className={`${prefix}-section`}>
                    <div className={`${prefix}-section-label`}>分析线索</div>
                    {!hasFacts && metrics.length > 0 && (
                      <div className={`${prefix}-hint`}>以下统计与线索用于辅助推断，不等同于已证实事实。</div>
                    )}
                    {clueItems.map((item: AnalysisEvidenceItem, idx) => {
                      const title = item.title || itemTypeLabel(item.source_type);
                      const snippet = item.snippet || '';
                      const reason = formatReason(item.reason) || itemTypeLabel(item.source_type);
                      return (
                        <div key={`clue-item-${key}-${idx}`} className={`${prefix}-entry`}>
                          <div className={`${prefix}-entry-title`}>{title}</div>
                          {snippet && snippet !== title && (
                            <div className={`${prefix}-entry-snippet`}>{snippet}</div>
                          )}
                          {reason && <div className={`${prefix}-entry-reason`}>{reason}</div>}
                        </div>
                      );
                    })}
                    {matchedEntries.map((item, idx) => (
                      <div key={`clue-match-${key}-${idx}`} className={`${prefix}-entry`}>
                        <div className={`${prefix}-entry-title`}>情绪相关片段</div>
                        {item.snippet && <div className={`${prefix}-entry-snippet`}>{item.snippet}</div>}
                        {item.matched_terms && item.matched_terms.length > 0 && (
                          <div className={`${prefix}-entry-reason`}>
                            关键词：{item.matched_terms.join('、')}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
