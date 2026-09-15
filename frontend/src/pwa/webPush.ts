/**
 * Web Push subscribe helpers. Permission only after explicit user gesture
 * and only when backend reports enabled+ready with a valid VAPID public key.
 *
 * iOS/iPad Safari tab must return needs_standalone BEFORE checking PushManager,
 * so ordinary tabs never touch registration.pushManager.
 */

import { registerServiceWorker } from './registerServiceWorker';

export type PushCapability =
  | 'unsupported'
  | 'needs_standalone'
  | 'ready'
  | 'permission_denied'
  | 'permission_default'
  | 'permission_granted'
  | 'backend_not_ready';

const USER_FACING_ERRORS = new Set([
  '手机通知尚未配置完成',
  'Service Worker 注册失败',
  '通知权限已被拒绝',
  '未授予通知权限',
  '请先添加到主屏幕后再启用通知',
  '当前环境不支持手机通知',
  '订阅信息不完整',
  '启用手机通知失败，请稍后重试',
]);

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output;
}

/** Uncompressed P-256 point is 65 bytes starting with 0x04. */
export function isValidVapidPublicKey(publicKey: string): boolean {
  if (!publicKey || typeof publicKey !== 'string') return false;
  try {
    const bytes = urlBase64ToUint8Array(publicKey.trim());
    return bytes.length === 65 && bytes[0] === 0x04;
  } catch {
    return false;
  }
}

export function isStandaloneDisplay(): boolean {
  if (typeof window === 'undefined') return false;
  const mq = window.matchMedia?.('(display-mode: standalone)')?.matches;
  const iosStandalone = Boolean((navigator as Navigator & { standalone?: boolean }).standalone);
  return Boolean(mq || iosStandalone);
}

export function isLikelyIos(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /iphone|ipad|ipod/i.test(navigator.userAgent);
}

function hasPushRuntimeApis(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  );
}

/**
 * Map any thrown value to a stable Chinese business message.
 * Never surface TypeError, stack traces, or browser internals.
 */
export function toUserFacingPushError(err: unknown): string {
  if (typeof err === 'string') {
    const trimmed = err.trim();
    if (USER_FACING_ERRORS.has(trimmed)) return trimmed;
  }
  if (err instanceof Error) {
    const msg = (err.message || '').trim();
    if (USER_FACING_ERRORS.has(msg)) return msg;
  }
  return '启用手机通知失败，请稍后重试';
}

export function detectPushCapability(): PushCapability {
  if (typeof window === 'undefined') return 'unsupported';
  // iPhone/iPad ordinary Safari tab: guide to Home Screen BEFORE PushManager checks.
  if (isLikelyIos() && !isStandaloneDisplay()) {
    return 'needs_standalone';
  }
  if (!hasPushRuntimeApis()) {
    return 'unsupported';
  }
  if (Notification.permission === 'denied') return 'permission_denied';
  if (Notification.permission === 'granted') return 'permission_granted';
  if (Notification.permission === 'default') return 'permission_default';
  return 'ready';
}

function pushManagerOf(reg: ServiceWorkerRegistration | null | undefined): PushManager | null {
  if (!reg) return null;
  const pm = (reg as ServiceWorkerRegistration & { pushManager?: PushManager }).pushManager;
  return pm ?? null;
}

export async function subscribeWebPush(
  vapidPublicKey: string,
  opts?: { forceResubscribe?: boolean },
): Promise<PushSubscription> {
  try {
    const capability = detectPushCapability();
    if (capability === 'needs_standalone') {
      throw new Error('请先添加到主屏幕后再启用通知');
    }
    if (capability === 'unsupported') {
      throw new Error('当前环境不支持手机通知');
    }
    if (!isValidVapidPublicKey(vapidPublicKey)) {
      throw new Error('手机通知尚未配置完成');
    }
    const reg = await registerServiceWorker();
    const pushManager = pushManagerOf(reg);
    if (!reg || !pushManager) {
      throw new Error('当前环境不支持手机通知');
    }
    // Must be called from a user gesture path — never on page load.
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      throw new Error(permission === 'denied' ? '通知权限已被拒绝' : '未授予通知权限');
    }
    const existing = await pushManager.getSubscription();
    if (existing && opts?.forceResubscribe) {
      try {
        await existing.unsubscribe();
      } catch {
        /* ignore */
      }
    } else if (existing && !opts?.forceResubscribe) {
      return existing;
    }
    return pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey) as BufferSource,
    });
  } catch (err) {
    throw new Error(toUserFacingPushError(err));
  }
}

export async function getCurrentPushSubscription(): Promise<PushSubscription | null> {
  try {
    const capability = detectPushCapability();
    if (capability === 'needs_standalone' || capability === 'unsupported') {
      return null;
    }
    const reg = await registerServiceWorker();
    const pushManager = pushManagerOf(reg);
    if (!pushManager) return null;
    return await pushManager.getSubscription();
  } catch {
    return null;
  }
}

export function subscriptionToJSON(sub: PushSubscription): {
  endpoint: string;
  expirationTime: number | null;
  keys: { p256dh: string; auth: string };
} {
  const raw = sub.toJSON();
  const endpoint = raw.endpoint || '';
  const p256dh = raw.keys?.p256dh || '';
  const auth = raw.keys?.auth || '';
  if (!endpoint || !p256dh || !auth) {
    throw new Error('订阅信息不完整');
  }
  return {
    endpoint,
    expirationTime: raw.expirationTime ?? null,
    keys: { p256dh, auth },
  };
}
