/** Session-only dismiss flags for today-plan review dialogs (per user + Beijing plan_date). */

const PREFIX = 'growthlog_today_plan_review_';

function key(userId: number | string, kind: 'urgent' | 'rollover', planDate: string): string {
  return `${PREFIX}${userId}_${kind}_${planDate}`;
}

export function isReviewDismissed(
  userId: number | string | null | undefined,
  kind: 'urgent' | 'rollover',
  planDate: string,
): boolean {
  if (userId == null || userId === '') return false;
  try {
    return sessionStorage.getItem(key(userId, kind, planDate)) === '1';
  } catch {
    return false;
  }
}

export function markReviewDismissed(
  userId: number | string | null | undefined,
  kind: 'urgent' | 'rollover',
  planDate: string,
): void {
  if (userId == null || userId === '') return;
  try {
    sessionStorage.setItem(key(userId, kind, planDate), '1');
  } catch {
    /* ignore */
  }
}

/** Clear all today-plan review dismiss flags (call on login/logout). */
export function clearTodayPlanReviewSession(): void {
  try {
    const toRemove: string[] = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const k = sessionStorage.key(i);
      if (k && k.startsWith(PREFIX)) toRemove.push(k);
    }
    toRemove.forEach((k) => sessionStorage.removeItem(k));
  } catch {
    /* ignore */
  }
}
