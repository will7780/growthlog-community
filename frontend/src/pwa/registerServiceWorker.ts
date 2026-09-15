/**
 * Register GrowthLog Service Worker (push only). No offline API cache.
 */

let registrationPromise: Promise<ServiceWorkerRegistration | null> | null = null;

export function canUseServiceWorker(): boolean {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!canUseServiceWorker()) return null;
  if (!registrationPromise) {
    registrationPromise = navigator.serviceWorker
      .register('/service-worker.js', { scope: '/' })
      .then((reg) => reg)
      .catch(() => null);
  }
  return registrationPromise;
}
