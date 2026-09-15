/**
 * Persist last AI chat session so reopen resumes the same thread.
 *
 * localStorage value semantics:
 * - key missing → first open / cleared → resume newest session
 * - "" → user clicked「新对话」→ stay blank until a message creates a session
 * - session id → resume that session when still present
 */

const LAST_SESSION_KEY = 'growthlog_ai_last_session_id';

export type AiSessionResumePreference =
  | { mode: 'missing' }
  | { mode: 'new' }
  | { mode: 'id'; sessionId: string };

export function getAiSessionResumePreference(): AiSessionResumePreference {
  try {
    const raw = localStorage.getItem(LAST_SESSION_KEY);
    if (raw === null) return { mode: 'missing' };
    if (!raw.trim()) return { mode: 'new' };
    return { mode: 'id', sessionId: raw.trim() };
  } catch {
    return { mode: 'missing' };
  }
}

export function setLastAiSessionId(sessionId: string): void {
  try {
    localStorage.setItem(LAST_SESSION_KEY, sessionId);
  } catch {
    /* ignore quota / private mode */
  }
}

/** User explicitly started a blank thread; do not auto-pick newest on reopen. */
export function markExplicitNewAiChat(): void {
  try {
    localStorage.setItem(LAST_SESSION_KEY, '');
  } catch {
    /* ignore */
  }
}

export function clearAiSessionResumePreference(): void {
  try {
    localStorage.removeItem(LAST_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

/** Resolve which session to open given API session list (newest first). */
export function pickResumeSessionId(
  sessions: Array<{ session_id: string }>,
  preference: AiSessionResumePreference = getAiSessionResumePreference(),
): string | null {
  if (!sessions.length) return null;
  if (preference.mode === 'new') return null;
  if (preference.mode === 'id') {
    const found = sessions.find((s) => s.session_id === preference.sessionId);
    if (found) return found.session_id;
  }
  return sessions[0]?.session_id ?? null;
}
