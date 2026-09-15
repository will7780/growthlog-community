/* GrowthLog minimal Service Worker — push + notificationclick only.
 * Do NOT add fetch handlers that cache API/user/AI/attachment responses.
 */
/* eslint-disable no-restricted-globals */
importScripts('/sw-url-safety.js');

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_err) {
    data = {};
  }
  const title = typeof data.title === 'string' && data.title.trim() ? data.title.trim() : 'GrowthLog';
  const body =
    typeof data.body === 'string' && data.body.trim()
      ? data.body.trim()
      : '你有一条 GrowthLog 提醒';
  const targetUrl = resolveSafeAppUrl(data.url, self.location.origin);
  // Stable tag: at-least-once delivery may retry; same tag collapses duplicate UI.
  const tag = typeof data.tag === 'string' && data.tag ? data.tag : 'growthlog-reminder';
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      data: { url: targetUrl },
      silent: false,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const raw = event.notification && event.notification.data ? event.notification.data.url : null;
  const targetUrl = resolveSafeAppUrl(raw, self.location.origin);
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client && client.url && client.url.startsWith(self.location.origin)) {
          client.focus();
          if ('navigate' in client) {
            return client.navigate(targetUrl);
          }
          return undefined;
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
      return undefined;
    }),
  );
});
