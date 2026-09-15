import { useCallback, useEffect, useState } from 'react';
import ConfirmDialog from '../common/ConfirmDialog';
import {
  disconnectNotion,
  getNotionStatus,
  notionErrorMessage,
  startNotionOAuth,
  syncNotionNow,
} from '../../api/notion';
import type { NotionStatus, NotionUiState } from '../../types/notion';

interface NotionConnectionPanelProps {
  onError?: (message: string) => void;
  onSuccess?: (message: string) => void;
}

const STATE_LABEL: Record<NotionUiState, string> = {
  unbound: '未绑定',
  authorizing: '授权中',
  syncing: '同步中',
  connected: '已连接',
  partial: '部分同步',
  reauth_required: '需要重新授权',
  failed: '同步失败',
  disabled: '未启用',
};

export default function NotionConnectionPanel({ onError, onSuccess }: NotionConnectionPanelProps) {
  const [status, setStatus] = useState<NotionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await getNotionStatus();
      setStatus(next);
    } catch (err) {
      onError?.(notionErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (status?.ui_state !== 'syncing') return undefined;
    const timer = window.setInterval(() => {
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [load, status?.ui_state]);

  const uiState: NotionUiState = authorizing ? 'authorizing' : status?.ui_state || 'unbound';

  const connect = async (reauth = false) => {
    if (busy || authorizing) return;
    setBusy(true);
    setAuthorizing(true);
    try {
      const result = await startNotionOAuth();
      if (!result.authorization_url.startsWith('https://api.notion.com/')) {
        throw new Error('授权地址无效');
      }
      window.location.assign(result.authorization_url);
    } catch (err) {
      setAuthorizing(false);
      onError?.(reauth ? '重新连接失败，请稍后重试' : notionErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const syncNow = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await syncNotionNow();
      onSuccess?.('已开始同步');
      await load();
    } catch (err) {
      onError?.(notionErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const confirmDisconnect = async () => {
    setBusy(true);
    try {
      await disconnectNotion();
      setConfirmOpen(false);
      setStatus(null);
      await load();
      onSuccess?.('已解除 Notion 连接');
    } catch (err) {
      onError?.(notionErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="notion-connection" aria-labelledby="notion-connection-title">
      <div className="label-manager__head">
        <div>
          <h2 id="notion-connection-title" className="profile-about__title">外部连接</h2>
          <p className="profile-about__subtitle">Notion</p>
        </div>
        <span className="notion-connection__badge">{STATE_LABEL[uiState]}</span>
      </div>

      {loading && <p className="notion-connection__copy">正在读取连接状态…</p>}

      {!loading && uiState === 'unbound' && (
        <>
          <p className="notion-connection__copy">
            可选绑定你的 Notion 工作区。GrowthLog 只会同步你在 Notion 授权页明确允许的页面，用于你自己的检索和讲解，不会写回 Notion。
          </p>
          <button
            type="button"
            className="entry-sheet-btn entry-sheet-btn--primary"
            onClick={() => void connect(false)}
            disabled={busy || authorizing}
          >
            {authorizing ? '正在跳转…' : '连接 Notion'}
          </button>
        </>
      )}

      {!loading && uiState === 'authorizing' && (
        <p className="notion-connection__copy">正在打开 Notion 授权页，请勿重复点击。</p>
      )}

      {!loading && uiState === 'syncing' && (
        <p className="notion-connection__copy">正在同步已授权页面，完成后可在检索鼠和讲解鼠中引用。</p>
      )}

      {!loading && (uiState === 'connected' || uiState === 'partial' || uiState === 'failed' || uiState === 'reauth_required') && status && (
        <>
          {uiState === 'partial' && (
            <p className="notion-connection__copy">部分页面尚未完成同步，已完成的页面仍可检索。</p>
          )}
          {uiState === 'failed' && (
            <p className="notion-connection__copy notion-connection__copy--error">同步失败，请稍后重试。</p>
          )}
          {uiState === 'reauth_required' && (
            <p className="notion-connection__copy">授权已失效，需要重新连接后才能继续同步。</p>
          )}
          <dl className="notion-connection__meta">
            <div>
              <dt>工作区</dt>
              <dd>{status.workspace_name || '已连接'}</dd>
            </div>
            <div>
              <dt>已发现</dt>
              <dd>{status.discovered_count ?? status.page_count ?? 0}</dd>
            </div>
            <div>
              <dt>已索引</dt>
              <dd>{status.indexed_count ?? status.page_count ?? 0}</dd>
            </div>
            <div>
              <dt>部分完成</dt>
              <dd>{status.partial_count ?? 0}</dd>
            </div>
            <div>
              <dt>待处理</dt>
              <dd>{status.pending_count ?? 0}</dd>
            </div>
            <div>
              <dt>最近同步</dt>
              <dd>{status.last_sync_completed_at ? status.last_sync_completed_at.replace('T', ' ').slice(0, 16) : '尚未完成'}</dd>
            </div>
          </dl>
          <div className="notion-connection__actions">
            {uiState === 'reauth_required' ? (
              <button
                type="button"
                className="entry-sheet-btn entry-sheet-btn--primary"
                onClick={() => void connect(true)}
                disabled={busy || authorizing}
              >
                重新连接
              </button>
            ) : (
              <>
                <button type="button" className="entry-sheet-btn entry-sheet-btn--primary" onClick={() => void syncNow()} disabled={busy}>
                  立即同步
                </button>
                <button type="button" className="entry-sheet-btn entry-sheet-btn--ghost" onClick={() => void connect(true)} disabled={busy || authorizing}>
                  调整授权页面
                </button>
                <button type="button" className="entry-sheet-btn entry-sheet-btn--ghost" onClick={() => setConfirmOpen(true)} disabled={busy}>
                  解除连接
                </button>
              </>
            )}
            {uiState === 'failed' && (
              <button type="button" className="entry-sheet-btn entry-sheet-btn--ghost" onClick={() => void syncNow()} disabled={busy}>
                重试
              </button>
            )}
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmOpen}
        title="解除 Notion 连接"
        message="解除后将删除 GrowthLog 中缓存的 Notion 文本和向量，且无法继续检索这些页面。"
        confirmLabel="解除连接"
        destructive
        loading={busy}
        onConfirm={() => void confirmDisconnect()}
        onClose={() => setConfirmOpen(false)}
      />
    </section>
  );
}
