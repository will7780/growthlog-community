/**
 * 环境感知日志工具 + 全局错误捕获
 */
const isDev = import.meta.env.DEV;

// 日志存储（用于调试）
const logStore: { level: string; args: unknown[]; time: string }[] = [];
const MAX_LOG_STORE = 100;

function storeLog(level: string, args: unknown[]) {
  const time = new Date().toISOString();
  logStore.push({ level, args, time });
  if (logStore.length > MAX_LOG_STORE) {
    logStore.shift();
  }
}

export const logger = {
  debug: (...args: unknown[]) => {
    if (isDev) {
      console.debug('[DEBUG]', ...args);
      storeLog('DEBUG', args);
    }
  },
  info: (...args: unknown[]) => {
    if (isDev) {
      console.log('[INFO]', ...args);
      storeLog('INFO', args);
    }
  },
  warn: (...args: unknown[]) => {
    console.warn('[WARN]', ...args);
    storeLog('WARN', args);
  },
  error: (...args: unknown[]) => {
    console.error('[ERROR]', ...args);
    storeLog('ERROR', args);
  },
  // 获取最近日志
  getRecentLogs: () => [...logStore],
  // 清除日志
  clearLogs: () => logStore.length = 0,
};

// ========== 全局错误捕获 ==========

// 1. 捕获未处理的 Promise  rejection
window.addEventListener('unhandledrejection', (event) => {
  const error = event.reason;
  logger.error('[UNHANDLED REJECTION]', error?.message || error, error?.stack || '');
});

// 2. 捕获全局 JavaScript 错误
window.addEventListener('error', (event) => {
  const error = event.error;
  logger.error('[GLOBAL ERROR]', error?.message || event.message, error?.stack || '');
});

// 3. 捕获资源加载失败（如图片、脚本）
window.addEventListener('load', () => {
  window.addEventListener('error', (event) => {
    if (event.target !== window) {
      logger.warn('[RESOURCE ERROR]', event.target);
    }
  });
});

// ========== API 请求日志增强 ==========

// 包装 fetch，记录所有 API 请求
const originalFetch = window.fetch;
window.fetch = async function(...args) {
  const [url, options = {}] = args;
  const method = (options.method || 'GET').toUpperCase();
  const startTime = Date.now();

  logger.debug(`[API →] ${method} ${url}`);

  try {
    const response = await originalFetch(...args);
    const duration = Date.now() - startTime;
    const status = response.status;

    if (status >= 400) {
      logger.error(`[API ←] ${method} ${url} [${status}] ${duration}ms`);
      // 尝试读取错误信息
      try {
        const clone = response.clone();
        const text = await clone.text();
        logger.error(`[API ERROR BODY] ${text.slice(0, 500)}`);
      } catch {}
    } else {
      logger.debug(`[API ←] ${method} ${url} [${status}] ${duration}ms`);
    }

    return response;
  } catch (error) {
    const duration = Date.now() - startTime;
    logger.error(`[API ×] ${method} ${url} [NETWORK ERROR] ${duration}ms`, error);
    throw error;
  }
};

export default logger;
