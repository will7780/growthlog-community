import type { AgentAnalysis, AnalysisEvidenceEntry, AnalysisEvidenceItem, AnalysisModuleResult } from '../../types/api';

const MODULE_LABELS: Record<string, string> = {
  timeline: '时间线',
  weekly_review: '本周复盘',
  monthly_review: '本月复盘',
  goal_progress: '目标推进',
  emotion_pattern: '情绪线索',
  knowledge_clusters: '知识主题',
};

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
  return '依据';
}

function moduleHasEvidence(module?: AnalysisModuleResult): boolean {
  if (!module) return false;
  return (module.evidence_entries?.length || 0) > 0 || (module.evidence_items?.length || 0) > 0;
}

function collectModules(analysis: AgentAnalysis) {
  return Object.entries(MODULE_LABELS)
    .map(([key, label]) => ({
      key,
      label,
      module: analysis[key as keyof AgentAnalysis] as AnalysisModuleResult | undefined,
    }))
    .filter(({ module }) => moduleHasEvidence(module));
}

interface AnalysisEvidenceBlockProps {
  analysis?: AgentAnalysis | null;
  variant?: 'desktop' | 'mobile';
  onEntryClick?: (entryId: number) => void;
}

export default function AnalysisEvidenceBlock({
  analysis,
  variant = 'desktop',
  onEntryClick,
}: AnalysisEvidenceBlockProps) {
  if (!analysis) return null;

  const modules = collectModules(analysis);
  if (modules.length === 0) return null;

  const isMobile = variant === 'mobile';

  return (
    <div className={isMobile ? 'ai-mp-agent-steps ai-mp-analysis-evidence' : 'growth-ai-agent-steps growth-ai-analysis-evidence'}>
      <div className={isMobile ? 'ai-mp-agent-title' : 'growth-ai-agent-steps-title'}>分析依据</div>
      {modules.map(({ key, label, module }) => (
        <div key={key} className={isMobile ? 'ai-mp-analysis-module' : 'growth-ai-analysis-module'}>
          <div className={isMobile ? 'ai-mp-analysis-module-title' : 'growth-ai-analysis-module-title'}>{label}</div>
          {(module?.evidence_entries || []).slice(0, 3).map((entry: AnalysisEvidenceEntry, idx) => {
            const title = entry.title || '相关记录';
            const snippet = entry.snippet || '';
            const reason = formatReason(entry.reason);
            const clickable = Boolean(entry.entry_id && onEntryClick);

            if (clickable) {
              return (
                <button
                  key={`entry-${key}-${idx}`}
                  type="button"
                  className={isMobile ? 'ai-mp-analysis-entry' : 'growth-ai-analysis-entry'}
                  onClick={() => onEntryClick?.(entry.entry_id as number)}
                >
                  <div className={isMobile ? 'ai-mp-analysis-entry-title' : 'growth-ai-analysis-entry-title'}>{title}</div>
                  {snippet && (
                    <div className={isMobile ? 'ai-mp-analysis-entry-snippet' : 'growth-ai-analysis-entry-snippet'}>{snippet}</div>
                  )}
                  {reason && (
                    <div className={isMobile ? 'ai-mp-analysis-entry-reason' : 'growth-ai-analysis-entry-reason'}>{reason}</div>
                  )}
                </button>
              );
            }

            return (
              <div key={`entry-${key}-${idx}`} className={isMobile ? 'ai-mp-analysis-entry ai-mp-analysis-entry--static' : 'growth-ai-analysis-entry growth-ai-analysis-entry--static'}>
                <div className={isMobile ? 'ai-mp-analysis-entry-title' : 'growth-ai-analysis-entry-title'}>{title}</div>
                {snippet && (
                  <div className={isMobile ? 'ai-mp-analysis-entry-snippet' : 'growth-ai-analysis-entry-snippet'}>{snippet}</div>
                )}
                {reason && (
                  <div className={isMobile ? 'ai-mp-analysis-entry-reason' : 'growth-ai-analysis-entry-reason'}>{reason}</div>
                )}
              </div>
            );
          })}
          {(module?.evidence_items || []).slice(0, 3).map((item: AnalysisEvidenceItem, idx) => {
            const title = item.title || itemTypeLabel(item.source_type);
            const snippet = item.snippet || '';
            const reason = formatReason(item.reason) || itemTypeLabel(item.source_type);
            return (
              <div
                key={`item-${key}-${idx}`}
                className={isMobile ? 'ai-mp-analysis-entry ai-mp-analysis-entry--static' : 'growth-ai-analysis-entry growth-ai-analysis-entry--static'}
              >
                <div className={isMobile ? 'ai-mp-analysis-entry-title' : 'growth-ai-analysis-entry-title'}>{title}</div>
                {snippet && snippet !== title && (
                  <div className={isMobile ? 'ai-mp-analysis-entry-snippet' : 'growth-ai-analysis-entry-snippet'}>{snippet}</div>
                )}
                {reason && (
                  <div className={isMobile ? 'ai-mp-analysis-entry-reason' : 'growth-ai-analysis-entry-reason'}>{reason}</div>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
