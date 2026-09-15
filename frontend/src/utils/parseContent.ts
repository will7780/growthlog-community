/**
 * 解析 content，提取标题和正文
 * 约定：content 第一行作为标题，剩余部分作为正文
 */

export interface ParsedContent {
  title: string;
  body: string;
}

export const SEGMENT_SEPARATOR = '\n\n---#SEG#---\n\n';

export interface ParsedSegmentContent {
  title: string;
  segments: string[];
}

/**
 * 解析 content，提取标题和正文
 */
export function parseContent(content: string): ParsedContent {
  const lines = content.split('\n');
  const title = lines[0] || '';
  const body = lines.slice(1).join('\n');
  return { title, body };
}

/**
 * 解析 content，提取标题和分段正文（兼容旧数据）
 */
export function parseSegmentContent(content: string): ParsedSegmentContent {
  const { title, body } = parseContent(content);
  const raw = body.trim();
  if (!raw) {
    return { title, segments: [] };
  }

  const segments = raw
    .split(SEGMENT_SEPARATOR)
    .map((segment) => segment.trim())
    .filter(Boolean);

  if (segments.length === 0) {
    return { title, segments: [raw] };
  }
  return { title, segments };
}

/**
 * 拼接标题和正文为 content
 * 如果 title 为空，只返回 body
 */
export function joinContent(title: string, body: string): string {
  if (!title.trim()) {
    return body;
  }
  return `${title}\n${body}`;
}
