const PUBLIC_VERSION_RE = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/;

export interface PublicReleaseNotes {
  version: string;
  title: string;
  changes: string[];
}

export async function getRuntimeVersion(signal?: AbortSignal): Promise<string | null> {
  try {
    const response = await fetch('/api/version', {
      method: 'GET',
      cache: 'no-store',
      signal,
    });
    if (!response.ok) return null;
    const payload = await response.json() as { version?: unknown };
    const value = String(payload.version || '').trim();
    return PUBLIC_VERSION_RE.test(value) ? value : null;
  } catch {
    return null;
  }
}

export async function getReleaseNotes(signal?: AbortSignal): Promise<PublicReleaseNotes | null> {
  try {
    const response = await fetch('/release-notes.json', {
      method: 'GET',
      cache: 'no-store',
      signal,
    });
    if (!response.ok) return null;
    const payload = await response.json() as Partial<PublicReleaseNotes>;
    const version = String(payload.version || '').trim();
    const title = String(payload.title || '').trim();
    const changes = Array.isArray(payload.changes)
      ? payload.changes.map((item) => String(item || '').trim()).filter(Boolean).slice(0, 8)
      : [];
    if (!PUBLIC_VERSION_RE.test(version) || !title || !changes.length) return null;
    return { version, title: title.slice(0, 80), changes: changes.map((item) => item.slice(0, 160)) };
  } catch {
    return null;
  }
}

export function formatRuntimeVersion(version: string | null): string {
  if (!version) return '版本信息暂不可用';
  if (version === '0.0.0-dev') return '开发版本';
  return `版本 ${version}`;
}