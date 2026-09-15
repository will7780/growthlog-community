/**
 * R11.3 检索员/讲解员正文渲染。
 *
 * 最小安全渲染：不使用 dangerouslySetInnerHTML / 不解析 markdown，只做
 * 纯文本换行 + 〔n〕引用切段（见 streamParse.splitCitationSegments）。
 * 点击引用按钮触发 onCitationClick，由调用方打开预览面板 / 底部抽屉。
 */
import { Fragment } from 'react';
import { splitCitationSegments } from './streamParse';
import type { StreamReferenceItem } from '../../api/aiStream';
import NotionOpenLink from './NotionOpenLink';

interface StreamMessageTextProps {
  text: string;
  onCitationClick?: (index: number) => void;
  hasReference?: (index: number) => boolean;
  references?: StreamReferenceItem[];
  sessionId?: string;
}

function renderTextWithBreaks(value: string, keyPrefix: string) {
  const lines = value.split('\n');
  return lines.map((line, idx) => (
    <Fragment key={`${keyPrefix}-${idx}`}>
      {idx > 0 && <br />}
      {line}
    </Fragment>
  ));
}

export default function StreamMessageText({ text, onCitationClick, hasReference, references, sessionId }: StreamMessageTextProps) {
  const segments = splitCitationSegments(text || '');
  return (
    <span className="stream-msg-text">
      {segments.map((seg, idx) => {
        if (seg.type === 'text') {
          return <Fragment key={idx}>{renderTextWithBreaks(seg.value, `t${idx}`)}</Fragment>;
        }
        const known = hasReference ? hasReference(seg.index) : true;
        const reference = references?.find((ref) => ref.display_index === seg.index);
        return (
          <Fragment key={idx}>
          <button
            key={idx}
            type="button"
            className={`stream-msg-citation${known ? '' : ' stream-msg-citation--unknown'}`}
            onClick={() => known && onCitationClick?.(seg.index)}
            disabled={!known}
            aria-label={`查看引用 ${seg.index}`}
          >
            {seg.raw}
          </button>
          {sessionId && reference?.source_type === 'notion_page' && <NotionOpenLink sessionId={sessionId} refToken={reference.ref_token} />}
          </Fragment>
        );
      })}
    </span>
  );
}
