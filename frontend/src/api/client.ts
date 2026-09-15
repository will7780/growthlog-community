/**
 * API Client
 * 使用 fetch 封装，自动处理 Token 和错误
 */
import { logger } from '../utils/logger';

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '';

export function getToken(): string | null {
  const token = localStorage.getItem('token');
  if (!token) return null;
  const cleanToken = token.trim().replace(/^Bearer\s+/i, '');
  return cleanToken || null;
}

export function saveToken(token: string): void {
  const cleanToken = token.trim().replace(/^Bearer\s+/i, '');
  if (!cleanToken) return;
  localStorage.setItem('token', cleanToken);
}

export function clearToken(): void {
  localStorage.removeItem('token');
}

function requiresAuth(endpoint: string): boolean {
  if (endpoint.includes('/api/auth/login')) return false;
  if (endpoint.includes('/api/health')) return false;
  return true;
}

const DEFAULT_TIMEOUT_MS = 10000;
const AI_TIMEOUT_MS = 60000;

function formatApiErrorDetail(detail: unknown, fallback = '请求失败'): string {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const data = detail as Record<string, unknown>;
    if (typeof data.message === 'string' && data.message.trim()) return data.message;
    const code = typeof data.code === 'string' ? data.code : '';
    if (code.includes('LLM') || code.includes('JUDGE') || code.includes('AI_')) {
      return 'AI 服务暂时不可用，请稍后重试';
    }
  }
  return fallback;
}

async function request<T>(
  endpoint: string,
  options: RequestInit = {},
  timeoutMs?: number
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  const token = getToken();
  const hasToken = !!token;
  const needsAuth = requiresAuth(endpoint);

  logger.debug(`请求: ${options.method || 'GET'} ${url}`);

  if (needsAuth && !hasToken) {
    if (!localStorage.getItem('token')) {
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
      throw new Error('未登录，请先登录');
    }
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const effectiveTimeout = timeoutMs ?? (endpoint.includes('/api/ai/') ? AI_TIMEOUT_MS : DEFAULT_TIMEOUT_MS);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), effectiveTimeout);

  try {
    const response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (response.status === 401) {
      let errorDetail = '未认证或 Token 已过期';
      try {
        const errorData = await response.json();
        errorDetail = formatApiErrorDetail(errorData.detail ?? errorData.message, errorDetail);
      } catch { /* ignore */ }

      if (needsAuth && hasToken) {
        clearToken();
        if (window.location.pathname !== '/login') {
          window.location.href = '/login';
        }
      } else if (needsAuth) {
        if (window.location.pathname !== '/login') {
          window.location.href = '/login';
        }
      }

      throw new Error(errorDetail);
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const errorMsg = formatApiErrorDetail(
        errorData.detail ?? errorData.message,
        `HTTP ${response.status}`,
      );
      logger.warn(`请求失败 [${response.status}]: ${url}`);
      const err = new Error(errorMsg) as Error & { status?: number; code?: string; retryAfterSeconds?: number };
      err.status = response.status;
      if (errorData.detail && typeof errorData.detail === 'object') {
        const detail = errorData.detail as Record<string, unknown>;
        const code = detail.code;
        if (typeof code === 'string') err.code = code;
        const retryAfter = detail.retry_after_seconds;
        if (typeof retryAfter === 'number' && Number.isFinite(retryAfter) && retryAfter > 0) {
          err.retryAfterSeconds = Math.ceil(retryAfter);
      }
        }
      throw err;
    }

    if (response.status === 204) {
      return undefined as T;
    }

    const data = await response.json();
    return data;
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('请求超时，请检查网络连接');
    }
    throw error;
  }
}

interface RequestOptions {
  timeoutMs?: number;
}

interface DownloadOptions extends RequestOptions {
  cache?: RequestCache;
  signal?: AbortSignal;
}

export function get<T>(endpoint: string, opts?: RequestOptions): Promise<T> {
  return request<T>(endpoint, { method: 'GET' }, opts?.timeoutMs);
}

export function post<T>(endpoint: string, data?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(endpoint, {
    method: 'POST',
    body: data ? JSON.stringify(data) : undefined,
  }, opts?.timeoutMs);
}

export function put<T>(endpoint: string, data?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(endpoint, {
    method: 'PUT',
    body: data ? JSON.stringify(data) : undefined,
  }, opts?.timeoutMs);
}

export function patch<T>(endpoint: string, data?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(endpoint, {
    method: 'PATCH',
    body: data ? JSON.stringify(data) : undefined,
  }, opts?.timeoutMs);
}

export function del<T>(endpoint: string, data?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(endpoint, {
    method: 'DELETE',
    body: data ? JSON.stringify(data) : undefined,
  }, opts?.timeoutMs);
}

export async function postForm<T>(
  endpoint: string,
  data: FormData,
  opts?: RequestOptions
): Promise<T> {
  const token = getToken();
  if (!token) {
    if (window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
    throw new Error('未登录，请先登录');
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  try {
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
      },
      body: data,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || errorData.message || `HTTP ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    clearTimeout(timeoutId);
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('请求超时，请检查网络连接');
    }
    throw error;
  }
}

export async function downloadBlob(endpoint: string, opts?: DownloadOptions): Promise<Blob> {
  const token = getToken();
  if (!token) {
    if (window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
    throw new Error('未登录，请先登录');
  }

  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const relayAbort = () => controller.abort();
  if (opts?.signal?.aborted) {
    controller.abort();
  } else {
    opts?.signal?.addEventListener('abort', relayAbort, { once: true });
  }

  try {
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
      },
      signal: controller.signal,
      cache: opts?.cache ?? 'default',
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.detail || errorData.message || `HTTP ${response.status}`);
    }

    return await response.blob();
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError' && timedOut) {
      throw new Error('请求超时，请检查网络连接');
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    opts?.signal?.removeEventListener('abort', relayAbort);
  }
}
