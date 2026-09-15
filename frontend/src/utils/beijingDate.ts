/**
 * Asia/Shanghai calendar date helpers for Todo / plan business dates.
 * Never use UTC ISO string slicing for YYYY-MM-DD business dates (UTC shift risk).
 */

const SHANGHAI_TZ = 'Asia/Shanghai';

/** Today's calendar date in Asia/Shanghai as YYYY-MM-DD. */
export function beijingTodayYmd(now: Date = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: SHANGHAI_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(now);
}

/** Add calendar days to a YYYY-MM-DD (or to Beijing today if omitted). */
export function beijingAddDaysYmd(days: number, fromYmd?: string): string {
  const base = fromYmd || beijingTodayYmd();
  const [y, m, d] = base.split('-').map((part) => Number(part));
  const utc = new Date(Date.UTC(y, m - 1, d));
  utc.setUTCDate(utc.getUTCDate() + days);
  const yyyy = utc.getUTCFullYear();
  const mm = String(utc.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(utc.getUTCDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/** Format a YYYY-MM-DD due date for Todo list display (今天/明天/M-D). */
export function formatBeijingDueLabel(dueDateYmd: string | null | undefined): string {
  if (!dueDateYmd) return '';
  const today = beijingTodayYmd();
  const tomorrow = beijingAddDaysYmd(1, today);
  if (dueDateYmd === today) return '今天';
  if (dueDateYmd === tomorrow) return '明天';
  const [, m, d] = dueDateYmd.split('-');
  return `${Number(m)}-${Number(d)}`;
}

/** Whole calendar days from Beijing today to dueDateYmd (negative = overdue). */
export function beijingDaysUntil(dueDateYmd: string | null | undefined): number | null {
  if (!dueDateYmd) return null;
  const today = beijingTodayYmd();
  const [ty, tm, td] = today.split('-').map(Number);
  const [dy, dm, dd] = dueDateYmd.split('-').map(Number);
  const t = Date.UTC(ty, tm - 1, td);
  const d = Date.UTC(dy, dm - 1, dd);
  return Math.round((d - t) / 86400000);
}
