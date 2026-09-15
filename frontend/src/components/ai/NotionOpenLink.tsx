import { useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { previewStreamReference } from '../../api/ai';

export function safeNotionUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443')) return null;
    if (url.hostname !== 'notion.so' && !url.hostname.endsWith('.notion.so')) return null;
    return url.href;
  } catch { return null; }
}

export default function NotionOpenLink({ sessionId, refToken }: { sessionId: string; refToken: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function open() {
    if (busy) return;
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    setBusy(true);
    setError('');
    try {
      const data = await previewStreamReference(sessionId, refToken);
      const url = safeNotionUrl(data.open_url || data.reference?.metadata?.open_url);
      if (!url) throw new Error('missing_url');
      if (tab) tab.location.replace(url);
      else window.location.assign(url);
    } catch {
      tab?.close();
      setError('无法打开 Notion 页面，请稍后重试');
    } finally { setBusy(false); }
  }
  return <span className="notion-open-link">
    <button type="button" onClick={open} disabled={busy} title="在 Notion 中打开" aria-label="在 Notion 中打开">
      <ExternalLink size={14} aria-hidden="true" />{busy ? '打开中…' : 'Notion'}
    </button>
    {error && <span role="alert">{error}</span>}
  </span>;
}
