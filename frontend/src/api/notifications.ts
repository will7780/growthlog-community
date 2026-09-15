import { del, get, patch, post } from './client';
import type {
  NotificationSettings,
  NotificationSettingsPatch,
  NotificationTestResponse,
  WebPushOkResponse,
  WebPushPublicKeyResponse,
  WebPushStatusResponse,
} from '../types/notification';

export async function getNotificationSettings(): Promise<NotificationSettings> {
  return get<NotificationSettings>('/api/notifications/settings');
}

export async function patchNotificationSettings(
  data: NotificationSettingsPatch,
): Promise<NotificationSettings> {
  return patch<NotificationSettings>('/api/notifications/settings', data);
}

export async function getWebPushPublicKey(): Promise<WebPushPublicKeyResponse> {
  return get<WebPushPublicKeyResponse>('/api/notifications/web-push/public-key');
}

export async function upsertWebPushSubscription(data: {
  endpoint: string;
  expirationTime?: number | null;
  keys: { p256dh: string; auth: string };
}): Promise<WebPushOkResponse> {
  return post<WebPushOkResponse>('/api/notifications/web-push/subscriptions', data);
}

export async function getWebPushSubscriptionStatus(endpoint: string): Promise<WebPushStatusResponse> {
  return post<WebPushStatusResponse>('/api/notifications/web-push/subscriptions/status', {
    endpoint,
  });
}

export async function deleteWebPushSubscription(endpoint: string): Promise<WebPushOkResponse> {
  return del<WebPushOkResponse>('/api/notifications/web-push/subscriptions', { endpoint });
}

export async function sendNotificationTest(
  endpoint: string,
): Promise<NotificationTestResponse> {
  return post<NotificationTestResponse>('/api/notifications/test', { endpoint });
}
