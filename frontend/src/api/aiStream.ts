/**
 * R11.3 检索员 / 讲解员真实 provider streaming 客户端。
 *
 * 用 fetch + ReadableStream 读取 `text/event-stream`，用 AbortController 取消；
 * SSE 帧解析复用 components/ai/streamParse.ts 中的纯函数（唯一实现，供
 * 单测和 usePersonaChat 共同依赖）。
 *
 * 不在这里做任何业务状态管理——只把网络字节流转换成结构化事件序列，
 * 交给 usePersonaChat 消费。
 */
import { API_BASE_URL, getToken } from './client';
import {
  createUtf8StreamDecoder,
  parseSseChunkBuffer,
  safeParseSseData,
  SSE_EVENT_DONE,
  SSE_EVENT_ERROR,
  SSE_EVENT_META,
  SSE_EVENT_REFERENCE,
  SSE_EVENT_SOURCE_CANDIDATE,
  SSE_EVENT_SOURCE_SET,
  SSE_EVENT_STATUS,
  SSE_EVENT_TEXT_DELTA,
} from '../components/ai/streamParse';
import type { Persona } from './ai';

/** 安全预览信息：流事件从不携带 entry_id / attachment_id / reference_key。 */
export interface StreamReferenceItem {
  display_index: number;
  ref_token: string;
  source_type: 'entry' | 'attachment' | 'todo' | 'notion_page';
  title: string;
  snippet: string;
  created_at?: string | null;
}

export type StreamCandidateItem = Omit<StreamReferenceItem, 'created_at'>;

export interface StreamMetaEvent {
  type: 'meta';
  request_id: string;
  session_id: string;
  persona: Persona;
}

export interface StreamStatusEvent {
  type: 'status';
  status: string;
  [extra: string]: unknown;
}

export interface StreamSourceCandidateEvent extends StreamCandidateItem {
  type: 'source_candidate';
}

export interface StreamSourceSetEvent {
  type: 'source_set';
  version: number;
  members: StreamReferenceItem[];
}

export interface StreamTextDeltaEvent {
  type: 'text_delta';
  text: string;
}

export interface StreamReferenceEvent extends StreamReferenceItem {
  type: 'reference';
}

export interface StreamDoneEvent {
  type: 'done';
  request_id?: string | null;
  message?: string | null;
  answer?: string | null;
  references?: StreamReferenceItem[];
  candidates?: StreamCandidateItem[];
  proposal_id?: number | null;
  base_version?: number | null;
  phase?: string | null;
  source_set_version?: number | null;
  idempotent?: boolean;
  expansion_topic?: string | null;
}

export interface StreamErrorEvent {
  type: 'error';
  code: string;
  message: string;
}

export type PersonaStreamEvent =
  | StreamMetaEvent
  | StreamStatusEvent
  | StreamSourceCandidateEvent
  | StreamSourceSetEvent
  | StreamTextDeltaEvent
  | StreamReferenceEvent
  | StreamDoneEvent
  | StreamErrorEvent;

export const CLIENT_ERROR_ABORTED = 'CLIENT_STREAM_ABORTED';
export const CLIENT_ERROR_PROTOCOL = 'CLIENT_STREAM_PROTOCOL_ERROR';
export const CLIENT_ERROR_NETWORK = 'CLIENT_STREAM_NETWORK_ERROR';
export const CLIENT_ERROR_UNAUTHENTICATED = 'CLIENT_STREAM_UNAUTHENTICATED';

/** 生成 8-64 位 `request_id`（后端契约：`^[A-Za-z0-9_-]{8,64}$`）。 */
export function generateRequestId(): string {
  const cryptoObj = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID().replace(/-/g, '');
  }
  let out = '';
  for (let i = 0; i < 32; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

async function* readSseStream(
  response: Response,
  signal: AbortSignal
): AsyncGenerator<PersonaStreamEvent, void, unknown> {
  if (!response.body) {
    yield { type: 'error', code: CLIENT_ERROR_PROTOCOL, message: '流式响应为空，请重试。' };
    return;
  }
  const reader = response.body.getReader();
  const decoder = createUtf8StreamDecoder();
  let buffer = '';
  try {
    while (true) {
      if (signal.aborted) return;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value);
      const { frames, rest } = parseSseChunkBuffer(buffer);
      buffer = rest;
      for (const frame of frames) {
        if (signal.aborted) return;
        const payload = safeParseSseData(frame.data);
        if (payload == null) {
          yield { type: 'error', code: CLIENT_ERROR_PROTOCOL, message: '流式协议异常，请重试。' };
          return;
        }
        const event = mapFrameToEvent(frame.event, payload);
        if (event) yield event;
        if (frame.event === SSE_EVENT_DONE || frame.event === SSE_EVENT_ERROR) return;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const { frames } = parseSseChunkBuffer(`${buffer}\n\n`);
      for (const frame of frames) {
        const payload = safeParseSseData(frame.data);
        if (payload) {
          const event = mapFrameToEvent(frame.event, payload);
          if (event) yield event;
        }
      }
    }
  } catch (err) {
    if (signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
    yield { type: 'error', code: CLIENT_ERROR_NETWORK, message: '网络中断，请重试。' };
  } finally {
    try {
      reader.cancel();
    } catch {
      /* ignore */
    }
  }
}

function mapFrameToEvent(eventName: string, payload: Record<string, unknown>): PersonaStreamEvent | null {
  switch (eventName) {
    case SSE_EVENT_META:
      return { ...payload, type: 'meta' } as unknown as StreamMetaEvent;
    case SSE_EVENT_STATUS:
      return { ...payload, type: 'status' } as unknown as StreamStatusEvent;
    case SSE_EVENT_SOURCE_CANDIDATE:
      return { ...payload, type: 'source_candidate' } as unknown as StreamSourceCandidateEvent;
    case SSE_EVENT_SOURCE_SET:
      return { ...payload, type: 'source_set' } as unknown as StreamSourceSetEvent;
    case SSE_EVENT_TEXT_DELTA:
      return { ...payload, type: 'text_delta' } as unknown as StreamTextDeltaEvent;
    case SSE_EVENT_REFERENCE:
      return { ...payload, type: 'reference' } as unknown as StreamReferenceEvent;
    case SSE_EVENT_DONE:
      return { ...payload, type: 'done' } as unknown as StreamDoneEvent;
    case SSE_EVENT_ERROR:
      return {
        type: 'error',
        code: String(payload.code || 'STREAM_PROTOCOL_ERROR'),
        message: String(payload.message || 'AI 服务暂时不可用，请稍后重试。'),
      };
    default:
      // 未知事件名：按协议约定安全忽略，不影响已知事件继续消费。
      return null;
  }
}

interface StreamTurnOptions {
  requestId: string;
  signal: AbortSignal;
  modelKey?: string | null;
}

async function* streamTurn(
  path: string,
  message: string,
  opts: StreamTurnOptions
): AsyncGenerator<PersonaStreamEvent, void, unknown> {
  const token = getToken();
  if (!token) {
    yield { type: 'error', code: CLIENT_ERROR_UNAUTHENTICATED, message: '未登录，请先登录。' };
    return;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({
        message,
        request_id: opts.requestId,
        model_key: opts.modelKey || undefined,
      }),
      signal: opts.signal,
    });
  } catch (err) {
    if (opts.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
      yield { type: 'error', code: CLIENT_ERROR_ABORTED, message: '生成已取消。' };
      return;
    }
    yield { type: 'error', code: CLIENT_ERROR_NETWORK, message: '网络连接失败，请检查网络后重试。' };
    return;
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = `请求失败（${response.status}）`;
    try {
      const errorData = await response.json();
      const detail = errorData?.detail ?? errorData?.message;
      if (detail && typeof detail === 'object') {
        code = String((detail as Record<string, unknown>).code || code);
        message = String((detail as Record<string, unknown>).message || message);
      } else if (typeof detail === 'string' && detail.trim()) {
        message = detail;
      }
    } catch {
      /* ignore body parse failure */
    }
    yield { type: 'error', code, message };
    return;
  }

  yield* readSseStream(response, opts.signal);
}

/** POST /api/ai/chat/sessions/{sessionId}/retrieve/stream */
export function streamRetrieverTurn(
  sessionId: string,
  message: string,
  opts: StreamTurnOptions
): AsyncGenerator<PersonaStreamEvent, void, unknown> {
  return streamTurn(`/api/ai/chat/sessions/${sessionId}/retrieve/stream`, message, opts);
}

/** POST /api/ai/chat/sessions/{sessionId}/explain/stream */
export function streamExplainerTurn(
  sessionId: string,
  message: string,
  opts: StreamTurnOptions
): AsyncGenerator<PersonaStreamEvent, void, unknown> {
  return streamTurn(`/api/ai/chat/sessions/${sessionId}/explain/stream`, message, opts);
}

/** POST /api/ai/chat/sessions/{sessionId}/explain/resume/stream — 确认后来源回答（流式）。 */
export async function* streamExplainerResume(
  sessionId: string,
  proposalId: number,
  opts: StreamTurnOptions
): AsyncGenerator<PersonaStreamEvent, void, unknown> {
  const token = getToken();
  if (!token) {
    yield { type: 'error', code: CLIENT_ERROR_UNAUTHENTICATED, message: '未登录，请先登录。' };
    return;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/ai/chat/sessions/${sessionId}/explain/resume/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({
        proposal_id: proposalId,
        request_id: opts.requestId,
        model_key: opts.modelKey || undefined,
      }),
      signal: opts.signal,
    });
  } catch (err) {
    if (opts.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
      yield { type: 'error', code: CLIENT_ERROR_ABORTED, message: '生成已取消。' };
      return;
    }
    yield { type: 'error', code: CLIENT_ERROR_NETWORK, message: '网络连接失败，请检查网络后重试。' };
    return;
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = `请求失败（${response.status}）`;
    try {
      const errorData = await response.json();
      const detail = errorData?.detail ?? errorData?.message;
      if (detail && typeof detail === 'object') {
        code = String((detail as Record<string, unknown>).code || code);
        message = String((detail as Record<string, unknown>).message || message);
      } else if (typeof detail === 'string' && detail.trim()) {
        message = detail;
      }
    } catch {
      /* ignore */
    }
    yield { type: 'error', code, message };
    return;
  }

  yield* readSseStream(response, opts.signal);
}
