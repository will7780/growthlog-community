import { useEffect, useMemo, useState } from 'react';
import { getWeeklyStats } from '../../api/todos';
import {
  confirmReview,
  extractErrorMessage,
  getDerivedContents,
  previewReview,
  type DerivedContentItem,
  type ReviewConfirmType,
  type ReviewPreviewResponse,
} from '../../api/ai';
import type { WeeklyStatsResponse } from '../../types/todo';

type InsightScope = 'recent_7d' | 'recent_30d';

const SCOPE_OPTIONS: Array<{ value: InsightScope; label: string; description: string }> = [
  { value: 'recent_7d', label: '本周', description: '最近 7 天记录' },
  { value: 'recent_30d', label: '本月', description: '最近 30 天记录' },
];

const SAVE_OPTIONS: Array<{ type: ReviewConfirmType; label: string }> = [
  { type: 'review', label: '保存复盘' },
  { type: 'action_plan', label: '保存行动计划' },
  { type: 'tag_suggestion', label: '保存标签建议' },
];

function scopeTitle(scope: InsightScope): string {
  return scope === 'recent_7d' ? '本周成长洞察' : '本月成长洞察';
}

function formatPercent(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return '0%';
  return `${Math.round(value)}%`;
}

function compactList(items?: string[] | null, limit = 4): string[] {
  return (items || []).filter(Boolean).slice(0, limit);
}

interface InsightsWorkspaceProps {
  onEntryOpen?: (entryId: number) => void;
}

export default function InsightsWorkspace({ onEntryOpen }: InsightsWorkspaceProps) {
  const [scope, setScope] = useState<InsightScope>('recent_7d');
  const [weeklyStats, setWeeklyStats] = useState<WeeklyStatsResponse | null>(null);
  const [preview, setPreview] = useState<ReviewPreviewResponse | null>(null);
  const [savedItems, setSavedItems] = useState<DerivedContentItem[]>([]);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [savingType, setSavingType] = useState<ReviewConfirmType | null>(null);
  const [loadingSaved, setLoadingSaved] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const evidenceCount = preview?.source_entry_ids?.length || 0;
  const insightSignals = useMemo(() => {
    if (!preview) return [];
    return [
      ...compactList(preview.trends, 3).map((text) => ({ type: '趋势', text })),
      ...compactList(preview.keywords, 3).map((text) => ({ type: '主题', text })),
    ].slice(0, 5);
  }, [preview]);

  const loadSavedItems = async () => {
    setLoadingSaved(true);
    try {
      const response = await getDerivedContents({
        type: 'review,action_plan,tag_suggestion',
        status: 'confirmed',
      });
      setSavedItems(response.items || []);
    } catch (err) {
      setError(extractErrorMessage(err, '已保存洞察加载失败'));
    } finally {
      setLoadingSaved(false);
    }
  };

  useEffect(() => {
    getWeeklyStats()
      .then(setWeeklyStats)
      .catch((err) => setError(extractErrorMessage(err, '本周统计加载失败')));
    loadSavedItems();
  }, []);

  const handleGeneratePreview = async () => {
    setLoadingPreview(true);
    setError('');
    setNotice('');
    try {
      const result = await previewReview({
        scope_type: scope,
        title: scopeTitle(scope),
        source_entry_ids: [],
        trends: [],
        keywords: [],
        suggestions: [],
      });
      setPreview(result);
    } catch (err) {
      setError(extractErrorMessage(err, '洞察生成失败'));
    } finally {
      setLoadingPreview(false);
    }
  };

  const handleSave = async (type: ReviewConfirmType) => {
    if (!preview) return;
    setSavingType(type);
    setError('');
    setNotice('');
    try {
      await confirmReview({
        ...preview,
        type,
        scope_type: preview.scope_type || scope,
        source_entry_ids: preview.source_entry_ids || [],
      });
      setNotice('已保存到洞察内容');
      await loadSavedItems();
    } catch (err) {
      setError(extractErrorMessage(err, '保存失败，请重新生成后再试'));
    } finally {
      setSavingType(null);
    }
  };

  return (
    <section className="growth-insights" aria-labelledby="growth-insights-title">
      <div className="growth-insights-head">
        <div>
          <h2 id="growth-insights-title" className="growth-insights-title">成长洞察</h2>
          <p className="growth-insights-copy">把记录、小要事和 AI 复盘整理成可确认的行动线索。</p>
        </div>
        <div className="growth-insights-scope" aria-label="洞察范围">
          {SCOPE_OPTIONS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`growth-insights-scope-btn ${scope === item.value ? 'growth-insights-scope-btn--active' : ''}`}
              onClick={() => setScope(item.value)}
              title={item.description}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="growth-insights-grid">
        <article className="growth-insights-stat">
          <div className="growth-insights-stat-label">小要事完成率</div>
          <div className="growth-insights-stat-value">{formatPercent(weeklyStats?.completion_rate)}</div>
          <div className="growth-insights-stat-meta">
            {weeklyStats ? `${weeklyStats.completed}/${weeklyStats.total} 已完成，${weeklyStats.pending} 待推进` : '统计加载中'}
          </div>
        </article>
        <article className="growth-insights-stat">
          <div className="growth-insights-stat-label">当前依据</div>
          <div className="growth-insights-stat-value">{evidenceCount}</div>
          <div className="growth-insights-stat-meta">生成后可查看来源记录并确认保存</div>
        </article>
        <article className="growth-insights-stat">
          <div className="growth-insights-stat-label">已保存洞察</div>
          <div className="growth-insights-stat-value">{savedItems.length}</div>
          <div className="growth-insights-stat-meta">{loadingSaved ? '同步中' : '复盘、行动计划和标签建议'}</div>
        </article>
      </div>

      <div className="growth-insights-actions">
        <button
          type="button"
          className="growth-insights-primary"
          onClick={handleGeneratePreview}
          disabled={loadingPreview}
        >
          {loadingPreview ? '生成中...' : `生成${scope === 'recent_7d' ? '周' : '月'}洞察`}
        </button>
        {preview && (
          <div className="growth-insights-save-actions">
            {SAVE_OPTIONS.map((item) => (
              <button
                key={item.type}
                type="button"
                className="growth-insights-secondary"
                onClick={() => handleSave(item.type)}
                disabled={savingType !== null || evidenceCount === 0}
              >
                {savingType === item.type ? '保存中...' : item.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {error && <div className="growth-insights-error">{error}</div>}
      {notice && <div className="growth-insights-notice">{notice}</div>}

      {preview ? (
        <div className="growth-insights-preview">
          <div className="growth-insights-preview-main">
            <div className="growth-insights-section-label">AI 推断</div>
            <h3 className="growth-insights-preview-title">{preview.title || scopeTitle(scope)}</h3>
            <p className="growth-insights-summary">{preview.summary}</p>

            {insightSignals.length > 0 && (
              <div className="growth-insights-signal-list">
                {insightSignals.map((item, index) => (
                  <div key={`${item.type}-${index}`} className="growth-insights-signal">
                    <span>{item.type}</span>
                    <strong>{item.text}</strong>
                  </div>
                ))}
              </div>
            )}

            {preview.suggestions.length > 0 && (
              <div className="growth-insights-suggestions">
                <div className="growth-insights-section-label">建议行动</div>
                {preview.suggestions.slice(0, 5).map((item, index) => (
                  <div key={`${item}-${index}`} className="growth-insights-suggestion">{item}</div>
                ))}
              </div>
            )}
          </div>

          <aside className="growth-insights-evidence">
            <div className="growth-insights-section-label">事实依据</div>
            {preview.source_entry_ids.length > 0 ? (
              preview.source_entry_ids.slice(0, 8).map((entryId, index) => (
                <button
                  key={entryId}
                  type="button"
                  className="growth-insights-evidence-item"
                  onClick={() => onEntryOpen?.(entryId)}
                >
                  <span>记录 {index + 1}</span>
                  <strong>打开来源</strong>
                </button>
              ))
            ) : (
              <div className="growth-insights-empty">当前范围内没有可保存的来源记录。</div>
            )}
          </aside>
        </div>
      ) : (
        <div className="growth-insights-empty-panel">
          <strong>先生成一版洞察预览</strong>
          <span>生成结果只作为预览，保存复盘或行动计划前需要你主动确认。</span>
        </div>
      )}

      <section className="growth-insights-saved" aria-label="已保存洞察">
        <div className="growth-insights-section-label">已保存</div>
        {savedItems.length === 0 ? (
          <div className="growth-insights-empty">还没有确认保存的复盘内容。</div>
        ) : (
          savedItems.slice(0, 6).map((item) => (
            <article key={item.id} className="growth-insights-saved-item">
              <div className="growth-insights-saved-top">
                <span>{item.type === 'review' ? '复盘' : item.type === 'action_plan' ? '行动计划' : '标签建议'}</span>
                <time>{item.created_at ? item.created_at.slice(0, 10) : ''}</time>
              </div>
              <strong>{item.title}</strong>
              <p>{item.content || item.title}</p>
            </article>
          ))
        )}
      </section>
    </section>
  );
}
