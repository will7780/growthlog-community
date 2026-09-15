/**
 * App 根级跨日审核：紧急优先，再普通 rollover。
 * 关闭仅 sessionStorage 抑制；visibilitychange / 日期变化会重新请求。
 * 紧急删除确认在同一 dialog 内分阶段，避免双 aria-modal。
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Zap } from 'lucide-react';
import {
  getUrgentPreview,
  confirmUrgentReview,
  getRolloverPreview,
  confirmRollover,
} from '../../api/todos';
import type {
  UrgentPreviewItem,
  RolloverPreviewItem,
  UrgentAction,
} from '../../types/todo';
import { useTodos } from '../../contexts/TodoContext';
import { useAuth } from '../../contexts/AuthContext';
import { isReviewDismissed, markReviewDismissed } from '../../utils/todayPlanSession';

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

type Phase = 'idle' | 'urgent' | 'rollover';

function isReviewStaleError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  const coded = err as Error & { status?: number; code?: string };
  if (coded.code === 'REVIEW_STALE') return true;
  if (coded.status === 409 && /日期或任务状态已变化|REVIEW_STALE/.test(err.message)) {
    return true;
  }
  return false;
}

export default function TodayPlanReviewHost() {
  const { refreshAll } = useTodos();
  const { user } = useAuth();
  const userId = user?.id;
  const [phase, setPhase] = useState<Phase>('idle');
  const [planDate, setPlanDate] = useState('');
  const [urgentItems, setUrgentItems] = useState<UrgentPreviewItem[]>([]);
  const [urgentActions, setUrgentActions] = useState<Record<number, UrgentAction>>({});
  const [rolloverItems, setRolloverItems] = useState<RolloverPreviewItem[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deleteStage, setDeleteStage] = useState(false);
  const [knownPlanDate, setKnownPlanDate] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const titleId = useId();

  const openRollover = useCallback(async (date: string) => {
    if (userId != null && isReviewDismissed(userId, 'rollover', date)) {
      setPhase('idle');
      return;
    }
    const preview = await getRolloverPreview();
    setPlanDate(preview.plan_date);
    if (!preview.items.length) {
      setPhase('idle');
      return;
    }
    setRolloverItems(preview.items);
    setSelectedIds(new Set(preview.items.map((item) => item.todo.id)));
    setPhase('rollover');
  }, [userId]);

  const runPreviewFlow = useCallback(async () => {
    setError('');
    setDeleteStage(false);
    try {
      const urgent = await getUrgentPreview();
      setKnownPlanDate(urgent.plan_date);
      setPlanDate(urgent.plan_date);
      if (
        urgent.items.length
        && (userId == null || !isReviewDismissed(userId, 'urgent', urgent.plan_date))
      ) {
        setUrgentItems(urgent.items);
        const actions: Record<number, UrgentAction> = {};
        urgent.items.forEach((item) => {
          actions[item.todo.id] = 'keep';
        });
        setUrgentActions(actions);
        setPhase('urgent');
        return;
      }
      await openRollover(urgent.plan_date);
    } catch {
      setPhase('idle');
    }
  }, [openRollover, userId]);

  useEffect(() => {
    if (userId == null) {
      setPhase('idle');
      return;
    }
    void runPreviewFlow();
  }, [runPreviewFlow, userId]);

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState !== 'visible') return;
      void (async () => {
        try {
          const urgent = await getUrgentPreview();
          if (knownPlanDate && urgent.plan_date !== knownPlanDate) {
            setKnownPlanDate(urgent.plan_date);
            await runPreviewFlow();
          }
        } catch {
          /* ignore */
        }
      })();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [knownPlanDate, runPreviewFlow]);

  const open = phase === 'urgent' || phase === 'rollover';

  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const timer = window.setTimeout(() => closeRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !loading) {
        event.preventDefault();
        if (deleteStage) {
          setDeleteStage(false);
          return;
        }
        handleDismiss();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const nodes = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      );
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialogRef.current.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialogRef.current.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      previouslyFocused.current?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, loading, deleteStage]);

  const handleDismiss = () => {
    if (!planDate) {
      setPhase('idle');
      setDeleteStage(false);
      return;
    }
    if (phase === 'urgent') {
      if (userId != null) markReviewDismissed(userId, 'urgent', planDate);
      setDeleteStage(false);
      setPhase('idle');
      void openRollover(planDate);
      return;
    }
    if (phase === 'rollover' && userId != null) {
      markReviewDismissed(userId, 'rollover', planDate);
    }
    setPhase('idle');
  };

  const submitUrgent = async () => {
    const decisions = urgentItems.map((item) => ({
      todo_id: item.todo.id,
      action: urgentActions[item.todo.id] || 'keep',
      expected_descendant_count: item.affected_descendant_count,
    }));
    setLoading(true);
    setError('');
    try {
      await confirmUrgentReview({ plan_date: planDate, decisions });
      if (userId != null) markReviewDismissed(userId, 'urgent', planDate);
      setDeleteStage(false);
      await refreshAll();
      setPhase('idle');
      await openRollover(planDate);
    } catch (err) {
      if (isReviewStaleError(err)) {
        setError('日期或任务状态已变化，正在刷新');
        setDeleteStage(false);
        await runPreviewFlow();
      } else {
        setError(err instanceof Error ? err.message : '紧急审核提交失败');
      }
    } finally {
      setLoading(false);
    }
  };

  const applyUrgent = async () => {
    const decisions = urgentItems.map((item) => ({
      todo_id: item.todo.id,
      action: urgentActions[item.todo.id] || 'keep',
      expected_descendant_count: item.affected_descendant_count,
    }));
    const hasDelete = decisions.some((d) => d.action === 'delete');
    if (hasDelete && !deleteStage) {
      setDeleteStage(true);
      return;
    }
    await submitUrgent();
  };

  const applyRollover = async (selected: number[]) => {
    setLoading(true);
    setError('');
    try {
      await confirmRollover({
        plan_date: planDate,
        reviewed_todo_ids: rolloverItems.map((item) => item.todo.id),
        selected_todo_ids: selected,
        expected_descendant_counts: Object.fromEntries(
          rolloverItems.map((item) => [item.todo.id, item.affected_descendant_count]),
        ),
      });
      if (userId != null) markReviewDismissed(userId, 'rollover', planDate);
      await refreshAll();
      setPhase('idle');
    } catch (err) {
      if (isReviewStaleError(err)) {
        setError('日期或任务状态已变化，正在刷新');
        await runPreviewFlow();
      } else {
        setError(err instanceof Error ? err.message : '跨日确认失败');
      }
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;

  const deleteCount = urgentItems
    .filter((item) => urgentActions[item.todo.id] === 'delete')
    .reduce((count, item) => count + 1 + item.affected_descendant_count, 0);

  return (
    <div className="today-review-overlay" role="presentation">
      <div
        ref={dialogRef}
        className="today-review-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header className="today-review-dialog__header">
          <h2 id={titleId}>
            {phase === 'urgent'
              ? (deleteStage ? '确认删除紧急任务' : '紧急任务审核')
              : '跨日任务安排'}
          </h2>
          <button
            ref={closeRef}
            type="button"
            className="today-review-dialog__close"
            onClick={handleDismiss}
            disabled={loading}
          >
            关闭
          </button>
        </header>

        {phase === 'urgent' && deleteStage && (
          <div className="today-review-dialog__body">
            <p className="today-review-dialog__hint">
              将永久删除 {deleteCount} 条紧急任务，此操作无法恢复。
            </p>
            {error ? <div className="today-review-dialog__error" role="alert">{error}</div> : null}
            <div className="today-review-dialog__footer">
              <button type="button" onClick={() => setDeleteStage(false)} disabled={loading}>
                返回
              </button>
              <button
                type="button"
                className="today-review-dialog__primary"
                onClick={() => void submitUrgent()}
                disabled={loading}
              >
                {loading ? '提交中…' : '确认删除'}
              </button>
            </div>
          </div>
        )}

        {phase === 'urgent' && !deleteStage && (
          <div className="today-review-dialog__body">
            <p className="today-review-dialog__hint">
              以下紧急任务需要先处理。保留将加入今日计划，不会自动修改截止日期；删除将永久移除。
            </p>
            <ul className="today-review-list">
              {urgentItems.map((item) => (
                <li key={item.todo.id} className="today-review-list__item">
                  <div className="today-review-list__main">
                    <Zap size={16} aria-hidden="true" />
                    <div>
                      <div className="today-review-list__title">{item.todo.content}</div>
                      <div className="today-review-list__meta">
                        {item.reasons.includes('overdue') ? '已过期' : null}
                        {item.reasons.includes('past_active_plan')
                          ? `昨日未完成${item.past_active_plan_date ? `（${item.past_active_plan_date}）` : ''}`
                          : null}
                        {item.affected_descendant_count
                          ? ' · 同时影响 ' + item.affected_descendant_count + ' 个子任务'
                          : null}
                      </div>
                    </div>
                  </div>
                  <div className="today-review-list__actions" role="radiogroup" aria-label={`处理 ${item.todo.content}`}>
                    <label>
                      <input
                        type="radio"
                        name={`urgent-${item.todo.id}`}
                        checked={(urgentActions[item.todo.id] || 'keep') === 'keep'}
                        onChange={() =>
                          setUrgentActions((prev) => ({ ...prev, [item.todo.id]: 'keep' }))
                        }
                      />
                      保留
                    </label>
                    <label>
                      <input
                        type="radio"
                        name={`urgent-${item.todo.id}`}
                        checked={urgentActions[item.todo.id] === 'delete'}
                        onChange={() =>
                          setUrgentActions((prev) => ({ ...prev, [item.todo.id]: 'delete' }))
                        }
                      />
                      删除
                    </label>
                  </div>
                </li>
              ))}
            </ul>
            {error ? <div className="today-review-dialog__error" role="alert">{error}</div> : null}
            <div className="today-review-dialog__footer">
              <button type="button" onClick={handleDismiss} disabled={loading}>稍后处理</button>
              <button type="button" className="today-review-dialog__primary" onClick={() => void applyUrgent()} disabled={loading}>
                {loading ? '提交中…' : '确认'}
              </button>
            </div>
          </div>
        )}

        {phase === 'rollover' && (
          <div className="today-review-dialog__body">
            <p className="today-review-dialog__hint">
              以下未完成任务来自过去的今日计划。默认全部加入今天；也可取消勾选或今天都不安排。
            </p>
            <ul className="today-review-list">
              {rolloverItems.map((item) => (
                <li key={item.todo.id} className="today-review-list__item">
                  <label className="today-review-list__check">
                    <input
                      type="checkbox"
                      checked={selectedIds.has(item.todo.id)}
                      onChange={(event) => {
                        setSelectedIds((prev) => {
                          const next = new Set(prev);
                          if (event.target.checked) next.add(item.todo.id);
                          else next.delete(item.todo.id);
                          return next;
                        });
                      }}
                    />
                    <span>
                      <span className="today-review-list__title">{item.todo.content}</span>
                      <span className="today-review-list__meta">
                        原计划日 {item.past_plan_date}
                        {item.affected_descendant_count
                          ? ` · 同时影响 ${item.affected_descendant_count} 个子任务`
                          : ''}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            {error ? <div className="today-review-dialog__error" role="alert">{error}</div> : null}
            <div className="today-review-dialog__footer">
              <button type="button" onClick={handleDismiss} disabled={loading}>关闭</button>
              <button
                type="button"
                onClick={() => void applyRollover([])}
                disabled={loading}
              >
                今天都不安排
              </button>
              <button
                type="button"
                className="today-review-dialog__primary"
                onClick={() => void applyRollover(Array.from(selectedIds))}
                disabled={loading}
              >
                {loading ? '提交中…' : '将所选加入今天'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
