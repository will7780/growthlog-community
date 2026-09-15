/* Shared SW URL safety helpers — importScripts from service-worker.js; also loaded by Node tests. */
/* eslint-disable no-restricted-globals */

var GROWTHLOG_DEFAULT_APP_PATH = '/app?tab=todos&review=1';
var GROWTHLOG_ALLOWED_PATH_PREFIXES = ['/app', '/login', '/profile', '/admin'];

function growthlogHasControlChars(value) {
  if (typeof value !== 'string') return true;
  for (var i = 0; i < value.length; i += 1) {
    var code = value.charCodeAt(i);
    if (code < 32 || code === 127) return true;
  }
  return false;
}

function growthlogPathAllowed(pathname) {
  if (typeof pathname !== 'string' || !pathname.startsWith('/')) return false;
  if (pathname.startsWith('/api')) return false;
  for (var i = 0; i < GROWTHLOG_ALLOWED_PATH_PREFIXES.length; i += 1) {
    var prefix = GROWTHLOG_ALLOWED_PATH_PREFIXES[i];
    if (pathname === prefix || pathname.startsWith(prefix + '/')) return true;
  }
  return false;
}

/**
 * Resolve a notification deep link against origin.
 * Returns absolute same-origin URL string, or default app review URL.
 */
function resolveSafeAppUrl(raw, origin) {
  var fallback = String(origin).replace(/\/$/, '') + GROWTHLOG_DEFAULT_APP_PATH;
  if (typeof raw !== 'string' || !raw) return fallback;
  if (growthlogHasControlChars(raw)) return fallback;
  if (raw.indexOf('\\') !== -1) return fallback;
  if (raw.indexOf('://') !== -1) return fallback;
  if (raw.startsWith('//')) return fallback;
  if (!raw.startsWith('/')) return fallback;
  try {
    var resolved = new URL(raw, origin);
    if (resolved.origin !== origin) return fallback;
    if (!growthlogPathAllowed(resolved.pathname)) return fallback;
    return resolved.href;
  } catch (_err) {
    return fallback;
  }
}

if (typeof self !== 'undefined') {
  self.resolveSafeAppUrl = resolveSafeAppUrl;
  self.GROWTHLOG_DEFAULT_APP_PATH = GROWTHLOG_DEFAULT_APP_PATH;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    resolveSafeAppUrl: resolveSafeAppUrl,
    GROWTHLOG_DEFAULT_APP_PATH: GROWTHLOG_DEFAULT_APP_PATH,
    growthlogPathAllowed: growthlogPathAllowed,
  };
}
