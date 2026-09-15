/**
 * R11.3 三角色流式 SSE 纯解析工具。
 *
 * 只做字符串/字节层解析，不发请求、不依赖 DOM/fetch，方便单元测试
 * （见 frontend/scripts/test_stream_parse.mjs）与 usePersonaChat 复用。
 *
 * 事件契约见 docs/00-主设计与升级计划.md §4.16.2：
 * meta / status / source_candidate / source_set / text_delta / reference / done / error
 */

export interface ParsedSseFrame {
  event: string;
  data: string;
}

/**
 * 从累积的字符串缓冲区中提取完整的 SSE frame（以空行 `\n\n` 结尾）。
 * 支持任意 chunk 边界：不完整的尾部保留在 rest 中，等待下一次 feed。
 */
export function parseSseChunkBuffer(buffer: string): { frames: ParsedSseFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n');
  const frames: ParsedSseFrame[] = [];
  let rest = normalized;
  let boundary = rest.indexOf('\n\n');
  while (boundary !== -1) {
    const rawFrame = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    const frame = parseOneFrame(rawFrame);
    if (frame) frames.push(frame);
    boundary = rest.indexOf('\n\n');
  }
  return { frames, rest };
}

function parseOneFrame(rawFrame: string): ParsedSseFrame | null {
  if (!rawFrame.trim()) return null;
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of rawFrame.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''));
    }
    // 其它字段（如 id:/retry:/注释行 `:`）当前协议未使用，忽略即可。
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n') };
}

/** data 字段按协议约定始终是单行 JSON；解析失败时返回 null，不抛异常。 */
export function safeParseSseData(raw: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * 增量 UTF-8 解码器：处理多字节字符被切分在两个网络 chunk 边界之间的情况。
 * 必须复用同一个实例贯穿整个流；结束时调用一次 `decode()`（不传参）冲刷剩余字节。
 */
export function createUtf8StreamDecoder(): {
  decode: (chunk?: Uint8Array | null) => string;
} {
  const decoder = new TextDecoder('utf-8', { fatal: false });
  return {
    decode(chunk?: Uint8Array | null): string {
      if (chunk == null) return decoder.decode();
      return decoder.decode(chunk, { stream: true });
    },
  };
}

const CITATION_PATTERN = /〔(\d+)〕/g;

/** 提取正文中的〔n〕引用编号，按首次出现顺序去重。 */
export function extractCitationIndices(text: string): number[] {
  const seen: number[] = [];
  const seenSet = new Set<number>();
  let match: RegExpExecArray | null;
  const re = new RegExp(CITATION_PATTERN);
  while ((match = re.exec(text || '')) !== null) {
    const idx = Number(match[1]);
    if (!Number.isFinite(idx) || seenSet.has(idx)) continue;
    seenSet.add(idx);
    seen.push(idx);
  }
  return seen;
}

export type CitationSegment =
  | { type: 'text'; value: string }
  | { type: 'citation'; index: number; raw: string };

/**
 * 安全正文分段：把纯文本按〔n〕引用切成 text/citation 片段，供渲染层逐段输出。
 * 不做任何 HTML 解析——调用方必须始终以纯文本节点渲染 text 片段
 * （不得使用 dangerouslySetInnerHTML）。
 */
export function splitCitationSegments(text: string): CitationSegment[] {
  const input = text || '';
  const segments: CitationSegment[] = [];
  let lastIndex = 0;
  const re = new RegExp(CITATION_PATTERN);
  let match: RegExpExecArray | null;
  while ((match = re.exec(input)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: 'text', value: input.slice(lastIndex, match.index) });
    }
    const idx = Number(match[1]);
    segments.push({ type: 'citation', index: idx, raw: match[0] });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < input.length) {
    segments.push({ type: 'text', value: input.slice(lastIndex) });
  }
  return segments;
}

// ---------------------------------------------------------------------------
// 事件名常量 — 与 backend/app/services/ai_stream_protocol.py 保持一致
// ---------------------------------------------------------------------------
export const SSE_EVENT_META = 'meta';
export const SSE_EVENT_STATUS = 'status';
export const SSE_EVENT_SOURCE_CANDIDATE = 'source_candidate';
export const SSE_EVENT_SOURCE_SET = 'source_set';
export const SSE_EVENT_TEXT_DELTA = 'text_delta';
export const SSE_EVENT_REFERENCE = 'reference';
export const SSE_EVENT_DONE = 'done';
export const SSE_EVENT_ERROR = 'error';

export const KNOWN_SSE_EVENTS: ReadonlySet<string> = new Set([
  SSE_EVENT_META,
  SSE_EVENT_STATUS,
  SSE_EVENT_SOURCE_CANDIDATE,
  SSE_EVENT_SOURCE_SET,
  SSE_EVENT_TEXT_DELTA,
  SSE_EVENT_REFERENCE,
  SSE_EVENT_DONE,
  SSE_EVENT_ERROR,
]);
