/**
 * Optional mobile Web Push settings (R10). Never displays endpoints/keys/VAPID private key.
 * Current-device state comes from endpoint-specific status API, not active_device_count alone.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Bell } from 'lucide-react';
import ConfirmDialog from '../common/ConfirmDialog';
import {
  deleteWebPushSubscription,
  getNotificationSettings,
  getWebPushPublicKey,
  getWebPushSubscriptionStatus,
  patchNotificationSettings,
  sendNotificationTest,
  upsertWebPushSubscription,
} from '../../api/notifications';
import type { NotificationSettings } from '../../types/notification';
import {
  detectPushCapability,
  getCurrentPushSubscription,
  isLikelyIos,
  isValidVapidPublicKey,
  subscribeWebPush,
  subscriptionToJSON,
  toUserFacingPushError,
} from '../../pwa/webPush';
import { registerServiceWorker } from '../../pwa/registerServiceWorker';

function formatCountdown(total: number): string {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export default function MobileNotificationPanel() {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState('');
  const [statusMsg, setStatusMsg] = useState('');
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [testCooldown, setTestCooldown] = useState(0);
  const [capability, setCapability] = useState(detectPushCapability());
  const [currentDeviceActive, setCurrentDeviceActive] = useState(false);
  const [activeDeviceCount, setActiveDeviceCount] = useState(0);
  const [hasLocalSubscription, setHasLocalSubscription] = useState(false);
  const [backendReady, setBackendReady] = useState(false);
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  const refreshDeviceStatus = useCallback(async () => {
    const nextCapability = detectPushCapability();
    setCapability(nextCapability);
    // Ordinary iOS Safari tab / unsupported browsers must never touch PushManager.
    if (nextCapability === 'needs_standalone' || nextCapability === 'unsupported') {
      setHasLocalSubscription(false);
      setCurrentDeviceActive(false);
      return;
    }
    const sub = await getCurrentPushSubscription();
    setHasLocalSubscription(Boolean(sub));
    if (!sub) {
      setCurrentDeviceActive(false);
      return;
    }
    // Precise status for this browser endpoint — never infer from account-wide count alone.
    const status = await getWebPushSubscriptionStatus(sub.endpoint);
    setCurrentDeviceActive(Boolean(status.current_device_active));
    setActiveDeviceCount(Number(status.active_device_count || 0));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextCapability = detectPushCapability();
      setCapability(nextCapability);
      if (nextCapability === 'needs_standalone' || nextCapability === 'unsupported') {
        // Still load privacy/sound notes; never register SW or probe subscription APIs.
        try {
          const data = await getNotificationSettings();
          setSettings(data);
          setActiveDeviceCount(Number(data.active_device_count || 0));
        } catch {
          /* notes optional when capability blocks push */
        }
        setHasLocalSubscription(false);
        setCurrentDeviceActive(false);
        setBackendReady(false);
        return;
      }
      await registerServiceWorker();
      const data = await getNotificationSettings();
      setSettings(data);
      setActiveDeviceCount(Number(data.active_device_count || 0));
      const keyInfo = await getWebPushPublicKey();
      setBackendReady(
        Boolean(keyInfo.enabled && keyInfo.ready && isValidVapidPublicKey(keyInfo.public_key)),
      );
      await refreshDeviceStatus();
    } catch (err) {
      setError(toUserFacingPushError(err) === '启用手机通知失败，请稍后重试' ? '加载失败' : toUserFacingPushError(err));
    } finally {
      setLoading(false);
    }
  }, [refreshDeviceStatus]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const t = window.setTimeout(() => closeBtnRef.current?.focus(), 0);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !confirmDisable) {
        event.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = previous;
      window.clearTimeout(t);
      window.removeEventListener('keydown', onKey);
    };
  }, [open, confirmDisable]);

  useEffect(() => {
    if (testCooldown <= 0) return;
    const timer = window.setInterval(() => {
      setTestCooldown((v) => Math.max(0, v - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [testCooldown]);

  const enabledOnDevice = currentDeviceActive && capability !== 'permission_denied';
  const subscriptionStale =
    capability !== 'unsupported' &&
    capability !== 'needs_standalone' &&
    capability !== 'permission_denied' &&
    hasLocalSubscription &&
    !currentDeviceActive;

  const handleEnable = async (forceResubscribe: boolean) => {
    setEnabling(true);
    setError('');
    setStatusMsg('');
    try {
      const keyInfo = await getWebPushPublicKey();
      if (!keyInfo.enabled || !keyInfo.ready || !isValidVapidPublicKey(keyInfo.public_key)) {
        setBackendReady(false);
        throw new Error('手机通知服务尚未就绪，请稍后重试');
      }
      setBackendReady(true);
      const sub = await subscribeWebPush(keyInfo.public_key, { forceResubscribe });
      const payload = subscriptionToJSON(sub);
      await upsertWebPushSubscription(payload);
      const next = await getNotificationSettings();
      setSettings(next);
      await refreshDeviceStatus();
      setStatusMsg('已在本设备启用手机通知');
    } catch (err) {
      await refreshDeviceStatus();
      setError(toUserFacingPushError(err));
    } finally {
      setEnabling(false);
    }
  };

  const handleSavePrefs = async () => {
    if (!settings) return;
    setLoading(true);
    setError('');
    try {
      const next = await patchNotificationSettings({
        reminders_enabled: settings.reminders_enabled,
        today_plan_time: settings.today_plan_time || '09:00',
        unfinished_time: settings.unfinished_time || '20:00',
        urgent_overdue_enabled: settings.urgent_overdue_enabled,
        quiet_hours_start: settings.quiet_hours_start || null,
        quiet_hours_end: settings.quiet_hours_end || null,
      });
      setSettings(next);
      setStatusMsg('偏好已保存');
    } catch (err) {
      setError('保存失败');
    } finally {
      setLoading(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setError('');
    setStatusMsg('');
    try {
      if (capability === 'needs_standalone' || capability === 'unsupported') {
        throw new Error(
          capability === 'needs_standalone'
            ? '请先添加到主屏幕后再启用通知'
            : '当前环境不支持手机通知',
        );
      }
      const sub = await getCurrentPushSubscription();
      if (!sub || !currentDeviceActive) {
        throw new Error('当前设备尚未启用通知');
      }
      await sendNotificationTest(sub.endpoint);
      setStatusMsg('测试通知已提交（是否听到声音由系统设置决定）');
      setTestCooldown(60);
    } catch (err) {
      const message = toUserFacingPushError(err);
      const raw = err instanceof Error ? err.message : '';
      if (raw.includes('429') || /rate|限流|冷却/i.test(raw) || /rate|限流|冷却/i.test(message)) {
        setTestCooldown(60);
        setError('发送过于频繁，请稍后重试');
      } else if (raw === '当前设备尚未启用通知') {
        setError('当前设备尚未启用通知');
      } else {
        setError(message === '启用手机通知失败，请稍后重试' ? '发送失败' : message);
      }
    } finally {
      setTesting(false);
    }
  };

  const handleDisableDevice = async () => {
    setLoading(true);
    setError('');
    try {
      const sub = await getCurrentPushSubscription();
      if (!sub) {
        setConfirmDisable(false);
        setCurrentDeviceActive(false);
        setHasLocalSubscription(false);
        return;
      }
      await deleteWebPushSubscription(sub.endpoint);
      try {
        await sub.unsubscribe();
      } catch {
        /* ignore browser unsubscribe errors */
      }
      const next = await getNotificationSettings();
      setSettings(next);
      await refreshDeviceStatus();
      setConfirmDisable(false);
      setStatusMsg('已关闭当前设备通知');
    } catch (err) {
      setError('关闭失败');
    } finally {
      setLoading(false);
    }
  };

  const canShowEnable =
    (capability === 'permission_default' ||
      capability === 'permission_granted' ||
      capability === 'ready') &&
    !enabledOnDevice &&
    !subscriptionStale;

  return (
    <>
      <button
        type="button"
        className="mobile-notification-entry"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
      >
        <Bell size={16} aria-hidden="true" />
        手机通知
      </button>

      {open && (
        <div className="mobile-notification-overlay" role="presentation">
          <div
            className="mobile-notification-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            ref={panelRef}
          >
            <div className="mobile-notification-panel__header">
              <h3 id={titleId}>手机通知</h3>
              <button
                type="button"
                className="mobile-notification-panel__close"
                ref={closeBtnRef}
                onClick={() => setOpen(false)}
              >
                关闭
              </button>
            </div>

            <p className="mobile-notification-panel__note">
              {settings?.privacy_note ||
                '默认只推送任务数量、紧急数量和 GrowthLog 链接，不发送 Todo 正文。'}
            </p>
            <p className="mobile-notification-panel__note">
              {settings?.sound_note ||
                '通知声音由 iOS/系统通知设置、静音与专注模式决定；无法指定自定义提示音。'}
            </p>

            {loading && !settings && <p className="mobile-notification-panel__status">加载中…</p>}
            {error && <p className="mobile-notification-panel__error">{error}</p>}
            {statusMsg && <p className="mobile-notification-panel__ok">{statusMsg}</p>}

            {capability === 'needs_standalone' && (
              <div className="mobile-notification-panel__block">
                <p className="mobile-notification-panel__status">请先添加到主屏幕后启用通知</p>
                <ol>
                  <li>用 Safari 打开 GrowthLog</li>
                  <li>点分享 → 添加到主屏幕</li>
                  <li>从主屏幕图标重新打开并登录后再启用</li>
                </ol>
              </div>
            )}

            {capability === 'unsupported' && (
              <div className="mobile-notification-panel__block">
                <p>当前环境暂不支持手机通知。请使用已添加到主屏幕的 Safari（iOS 16.4+）或其他支持的浏览器后重试。</p>
              </div>
            )}

            {capability === 'permission_denied' && (
              <div className="mobile-notification-panel__block">
                <p>通知权限已被拒绝。请到系统设置中为 GrowthLog 打开通知权限后重试。</p>
              </div>
            )}

            {!loading &&
              settings &&
              !backendReady &&
              capability !== 'unsupported' &&
              capability !== 'needs_standalone' &&
              capability !== 'permission_denied' &&
              !enabledOnDevice && (
                <div className="mobile-notification-panel__block">
                  <p className="mobile-notification-panel__status">
                    手机通知服务尚未就绪（backend_not_ready）。请稍后重试，不会自动申请通知权限。
                  </p>
                </div>
              )}

            {subscriptionStale && backendReady && (
              <div className="mobile-notification-panel__block">
                <p className="mobile-notification-panel__error">
                  当前设备订阅已失效或属于其他状态，需要重新启用后才能接收推送。
                </p>
                <button
                  type="button"
                  className="mobile-notification-panel__primary"
                  onClick={() => void handleEnable(true)}
                  disabled={enabling || loading}
                >
                  {enabling ? '正在启用…' : '重新启用手机通知'}
                </button>
              </div>
            )}

            {canShowEnable && backendReady && (
              <div className="mobile-notification-panel__block">
                <p>可选功能：启用后可在应用关闭时收到今日计划与紧急任务数量提醒。</p>
                {isLikelyIos() && (
                  <p className="mobile-notification-panel__status">请确认已从主屏幕图标打开 GrowthLog。</p>
                )}
                <button
                  type="button"
                  className="mobile-notification-panel__primary"
                  onClick={() => void handleEnable(false)}
                  disabled={enabling || loading}
                >
                  {enabling ? '正在启用…' : '启用手机通知'}
                </button>
              </div>
            )}

            {enabledOnDevice && settings && (
              <div className="mobile-notification-panel__block">
                <p className="mobile-notification-panel__ok">
                  当前设备已启用
                  {activeDeviceCount > 1 ? `（账号共 ${activeDeviceCount} 台设备）` : ''}
                </p>
                <label className="mobile-notification-field">
                  <span>启用提醒</span>
                  <input
                    type="checkbox"
                    checked={!!settings.reminders_enabled}
                    onChange={(e) =>
                      setSettings({ ...settings, reminders_enabled: e.target.checked })
                    }
                  />
                </label>
                <label className="mobile-notification-field">
                  <span>今日计划提醒</span>
                  <input
                    type="time"
                    value={settings.today_plan_time || '09:00'}
                    onChange={(e) => setSettings({ ...settings, today_plan_time: e.target.value })}
                  />
                </label>
                <label className="mobile-notification-field">
                  <span>晚间未完成提醒</span>
                  <input
                    type="time"
                    value={settings.unfinished_time || '20:00'}
                    onChange={(e) => setSettings({ ...settings, unfinished_time: e.target.value })}
                  />
                </label>
                <label className="mobile-notification-field">
                  <span>紧急逾期提醒</span>
                  <input
                    type="checkbox"
                    checked={!!settings.urgent_overdue_enabled}
                    onChange={(e) =>
                      setSettings({ ...settings, urgent_overdue_enabled: e.target.checked })
                    }
                  />
                </label>
                <label className="mobile-notification-field">
                  <span>免打扰开始</span>
                  <input
                    type="time"
                    value={settings.quiet_hours_start || ''}
                    onChange={(e) =>
                      setSettings({ ...settings, quiet_hours_start: e.target.value || null })
                    }
                  />
                </label>
                <label className="mobile-notification-field">
                  <span>免打扰结束</span>
                  <input
                    type="time"
                    value={settings.quiet_hours_end || ''}
                    onChange={(e) =>
                      setSettings({ ...settings, quiet_hours_end: e.target.value || null })
                    }
                  />
                </label>
                <div className="mobile-notification-actions">
                  <button type="button" onClick={() => void handleSavePrefs()} disabled={loading}>
                    保存设置
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleTest()}
                    disabled={testing || loading || testCooldown > 0}
                  >
                    {testing
                      ? '发送中…'
                      : testCooldown > 0
                        ? `${formatCountdown(testCooldown)} 后可重试`
                        : '发送测试通知'}
                  </button>
                  <button
                    type="button"
                    className="mobile-notification-actions__danger"
                    onClick={() => setConfirmDisable(true)}
                    disabled={loading}
                  >
                    关闭当前设备通知
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmDisable}
        title="关闭当前设备通知？"
        message="关闭后本设备不再接收推送，需要重新启用。其他设备不受影响。"
        confirmLabel="确认关闭"
        onConfirm={() => void handleDisableDevice()}
        onClose={() => setConfirmDisable(false)}
      />
    </>
  );
}
