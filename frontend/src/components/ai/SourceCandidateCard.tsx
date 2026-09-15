/**
 * 讲解员来源候选卡片：勾选后通过 opaque ref_token 确认 / 取消。
 * 不回传 reference_key / entry_id / attachment_id。
 */
import { useState } from 'react';
import { Check, X, Loader2 } from 'lucide-react';
import type { SourceCandidateMessage } from '../../hooks/usePersonaChat';

interface SourceCandidateCardProps {
  candidateMsg: SourceCandidateMessage;
  onConfirm: (msg: SourceCandidateMessage, selectedRefTokens: string[]) => void;
  onCancel: (msg: SourceCandidateMessage) => void;
}

function sourceLabel(sourceType: string): string {
  return sourceType === 'attachment' ? '附件' : sourceType === 'todo' ? '小要事' : sourceType === 'notion_page' ? 'Notion' : '记录';
}

export default function SourceCandidateCard({ candidateMsg, onConfirm, onCancel }: SourceCandidateCardProps) {
  const [checked, setChecked] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    candidateMsg.candidates.forEach((c) => {
      init[c.ref_token] = true;
    });
    return init;
  });

  const resolved = candidateMsg.resolution;
  const phaseLabel = candidateMsg.phase === 'awaiting_expansion_confirm' ? '新增来源候选' : '来源候选';

  return (
    <div
      className={`source-candidate-card${resolved ? ` source-candidate-card--${resolved}` : ''}`}
      data-testid="source-candidate-card"
    >
      <div className="source-candidate-card__title">{phaseLabel}</div>
      {candidateMsg.phase === 'awaiting_expansion_confirm' && candidateMsg.expansionTopic ? (
        <div className="source-candidate-card__topic" data-testid="source-candidate-topic">
          补充主题：{candidateMsg.expansionTopic}
        </div>
      ) : null}
      {candidateMsg.message && <div className="source-candidate-card__copy">{candidateMsg.message}</div>}

      <ul className="source-candidate-card__list">
        {candidateMsg.candidates.map((candidate) => (
          <li key={candidate.ref_token} className="source-candidate-card__item">
            <label className="source-candidate-card__check">
              <input
                type="checkbox"
                checked={Boolean(checked[candidate.ref_token])}
                disabled={Boolean(resolved)}
                onChange={() =>
                  setChecked((prev) => ({ ...prev, [candidate.ref_token]: !prev[candidate.ref_token] }))
                }
              />
              <span className="source-candidate-card__item-title">
                〔{candidate.display_index}〕 {candidate.title || '参考记录'}
              </span>
            </label>
            <div className="source-candidate-card__item-meta">
              {sourceLabel(candidate.source_type)}
              {candidate.snippet ? ` · ${candidate.snippet}` : ''}
            </div>
          </li>
        ))}
      </ul>

      {!resolved && (
        <>
          <div className="source-candidate-card__hint">请勾选要纳入讲解的来源，然后确认。</div>
          {candidateMsg.actionError && (
            <div className="source-candidate-card__error">{candidateMsg.actionError}</div>
          )}
          <div className="source-candidate-card__actions">
            <button
              type="button"
              className="source-candidate-card__btn"
              data-testid="source-candidate-cancel"
              disabled={candidateMsg.actionPending}
              onClick={() => onCancel(candidateMsg)}
            >
              {candidateMsg.actionPending ? <Loader2 size={14} className="source-candidate-card__spin" aria-hidden="true" /> : <X size={14} aria-hidden="true" />}
              取消
            </button>
            <button
              type="button"
              className="source-candidate-card__btn source-candidate-card__btn--primary"
              data-testid="source-candidate-confirm"
              disabled={
                candidateMsg.actionPending
                || !Object.values(checked).some(Boolean)
              }
              onClick={() =>
                onConfirm(
                  candidateMsg,
                  Object.entries(checked)
                    .filter(([, on]) => on)
                    .map(([token]) => token),
                )
              }
            >
              {candidateMsg.actionPending ? <Loader2 size={14} className="source-candidate-card__spin" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
              确认来源
            </button>
          </div>
        </>
      )}
      {resolved === 'confirmed' && <div className="source-candidate-card__status">已确认</div>}
      {resolved === 'cancelled' && <div className="source-candidate-card__status">已取消</div>}
    </div>
  );
}
