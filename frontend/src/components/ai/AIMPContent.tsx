/**
 * 移动端 AI 助手内容组件（R11.3 三角色版）
 * 作为 Tab 内容直接显示，而非覆盖层；流式业务逻辑全部委托给 usePersonaChat。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bot, Loader2, Send, Square } from 'lucide-react';
import {
  getAgentPendingActions,
  confirmAgentPendingAction,
  rejectAgentPendingAction,
} from '../../api/ai';
import type { AgentPendingActionItem, ReferenceItem } from '../../api/ai';
import type { EntryWithChildrenResponse } from '../../types/api';
import { getEntry } from '../../api/entries';
import AnalysisEvidenceBlock from './AnalysisEvidenceBlock';
import AnalysisDetailPanel, { hasAnalysisDetailContent } from './AnalysisDetailPanel';
import AgentMemoryPanel from './AgentMemoryPanel';
import ReferenceList from './ReferenceList';
import ReferenceDetail from './ReferenceDetail';
import ConfirmDialog from '../common/ConfirmDialog';
import { getReferenceCardKey } from './referenceDisplay';
import { OrganizeDiffPanel, OrganizeScopeBar, type OrganizeScopeState } from './OrganizeControls';
import { usePersonaChat, type LegacyMessage, type StreamChatMessage } from '../../hooks/usePersonaChat';
import PersonaPicker from './PersonaPicker';
import PersonaAvatar from './PersonaAvatar';
import { personaMeta } from './personaMeta';
import { ORGANIZER_MAINTENANCE_MESSAGE } from '../../config/aiPersonaAvailability';
import StreamMessageText from './StreamMessageText';
import StreamReferenceChips from './StreamReferenceChips';
import StreamReferencePreview from './StreamReferencePreview';
import SourceCandidateCard from './SourceCandidateCard';
import LockedSourcesBar from './LockedSourcesBar';
import ExpandTopicDialog from './ExpandTopicDialog';

function sessionListLabel(sessionTitle: string | null | undefined, lastMessage: string): string {
  if (sessionTitle && sessionTitle.trim()) return sessionTitle.trim();
  return lastMessage || '新对话';
}

export default function AIMPContent() {
  const navigate = useNavigate();
  const expandBtnRef = useRef<HTMLButtonElement | null>(null) as React.MutableRefObject<HTMLButtonElement | null>;
  const organizePreviewSessionRef = useRef<string | null>(null);
  const chat = usePersonaChat();

  const [input, setInput] = useState('');
  const [pendingActions, setPendingActions] = useState<AgentPendingActionItem[]>([]);
  const [showSessions, setShowSessions] = useState(false);
  const [analysisDetailIndex, setAnalysisDetailIndex] = useState<number | null>(null);
  const [memoryPanelOpen, setMemoryPanelOpen] = useState(false);
  const [deleteSessionId, setDeleteSessionId] = useState<string | null>(null);
  const [deleteSessionLoading, setDeleteSessionLoading] = useState(false);
  const [deleteSessionError, setDeleteSessionError] = useState('');
  const [organizeScope, setOrganizeScope] = useState<OrganizeScopeState>({
    days: 30,
    labelCodes: [],
    entryIds: [],
    allowedActions: ['create_summary', 'suggest_tag', 'create_action_plan', 'rewrite_content', 'memory_note'],
  });

  const [selectedReferenceKey, setSelectedReferenceKey] = useState<string | null>(null);
  const [referenceEntry, setReferenceEntry] = useState<EntryWithChildrenResponse | null>(null);
  const [referenceLoading, setReferenceLoading] = useState(false);
  const [referenceError, setReferenceError] = useState('');

  const legacyReferences = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i -= 1) {
      const msg = chat.messages[i];
      if (msg.kind === 'legacy' && msg.role === 'assistant' && msg.references && msg.references.length > 0) {
        return msg.references;
      }
    }
    return [] as ReferenceItem[];
  }, [chat.messages]);

  const selectedReference = useMemo(() => {
    if (!selectedReferenceKey) return null;
    return legacyReferences.find((ref, index) => getReferenceCardKey(ref, index) === selectedReferenceKey) || null;
  }, [legacyReferences, selectedReferenceKey]);

  const closeLegacyReference = () => {
    setSelectedReferenceKey(null);
    setReferenceEntry(null);
    setReferenceError('');
    setReferenceLoading(false);
  };

  const handleOrganizePreviewSource = useCallback((refToken: string, previewSessionId?: string) => {
    const sessionId = previewSessionId || chat.sessionId;
    if (!sessionId) return;
    organizePreviewSessionRef.current = previewSessionId || null;
    chat.openReferencePreview({
      display_index: 0,
      ref_token: refToken,
      source_type: 'entry',
      title: '',
      snippet: '',
    });
  }, [chat]);

  const closeStreamReferencePreview = useCallback(() => {
    organizePreviewSessionRef.current = null;
    chat.closeReferencePreview();
  }, [chat]);

  useEffect(() => {
    closeLegacyReference();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.sessionId]);

  useEffect(() => {
    const sid = chat.sessionId || undefined;
    getAgentPendingActions({ session_id: sid, status: 'pending' })
      .then(setPendingActions)
      .catch(() => setPendingActions([]));
  }, [chat.sessionId]);

  const handleOpenLegacyReference = async (reference: ReferenceItem, index: number) => {
    setSelectedReferenceKey(getReferenceCardKey(reference, index));
    setReferenceEntry(null);
    setReferenceError('');
    setReferenceLoading(true);
    try {
      const targetId = reference.root_entry_id || reference.entry_id;
      if (!targetId) throw new Error('该来源请在检索鼠或讲解鼠的引用预览中查看');
      setReferenceEntry(await getEntry(targetId));
    } catch (err) {
      setReferenceError(err instanceof Error ? err.message : '完整记录加载失败');
    } finally {
      setReferenceLoading(false);
    }
  };

  const handleJumpToEntry = (entryId: number) => {
    navigate(`/app?entry_id=${entryId}`);
  };

  const requestDeleteSession = (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleteSessionError('');
    setDeleteSessionId(sid);
  };

  const handleDeleteSession = async () => {
    if (!deleteSessionId || deleteSessionLoading) return;
    setDeleteSessionLoading(true);
    setDeleteSessionError('');
    try {
      await chat.deleteSession(deleteSessionId);
      setDeleteSessionId(null);
      setShowSessions(false);
    } catch (err) {
      setDeleteSessionError(err instanceof Error ? err.message : '删除会话失败');
    } finally {
      setDeleteSessionLoading(false);
    }
  };

  const handleConfirmPendingAction = async (actionId: number) => {
    try {
      await confirmAgentPendingAction(actionId);
      setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
    } catch {
      /* 静默处理 */
    }
  };

  const handleRejectPendingAction = async (actionId: number) => {
    try {
      await rejectAgentPendingAction(actionId);
      setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
    } catch {
      /* 静默处理 */
    }
  };

  const pendingActionTitle = (action: AgentPendingActionItem) => {
    if (action.action_type === 'create_todo') return '创建小要事';
    if (action.action_type === 'create_memory') return '保存个性设置';
    if (action.action_type === 'update_memory') return '更新个性设置';
    return '保存总结记录';
  };

  const pendingActionSummary = (action: AgentPendingActionItem) => {
    const payload = action.payload_json || {};
    const content = payload.content || payload.summary || payload.title || '';
    if (typeof content === 'string' && content.trim()) return content.trim();
    return JSON.stringify(payload).slice(0, 140);
  };

  const handleSend = () => {
    const text = input;
    if (!text.trim() || !chat.canSend) return;
    setInput('');
    void chat.sendMessage(text, {
      days: organizeScope.days,
      labelCodes: organizeScope.labelCodes,
      entryIds: organizeScope.entryIds,
      allowedActions: organizeScope.allowedActions,
    });
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const legacyAnalysisMessages = chat.messages.filter((m): m is LegacyMessage => m.kind === 'legacy');
  const meta = personaMeta(chat.persona);
  const legacyBanner = chat.sessionMeta?.legacy;

  if (selectedReference) {
    return (
      <div className="ai-mp-container">
        <ReferenceDetail
          reference={selectedReference}
          entry={referenceEntry}
          loading={referenceLoading}
          error={referenceError}
          onClose={closeLegacyReference}
          onOpenEntry={handleJumpToEntry}
          variant="mobile"
        />
      </div>
    );
  }

  if (chat.activeReferencePreview) {
    return (
      <div className="ai-mp-container">
        {(organizePreviewSessionRef.current || chat.sessionId) && (
          <StreamReferencePreview
            reference={chat.activeReferencePreview}
            sessionId={organizePreviewSessionRef.current || chat.sessionId || ''}
            onClose={closeStreamReferencePreview}
            variant="mobile"
          />
        )}
      </div>
    );
  }

  return (
    <div className="ai-mp-container">
      <div className="ai-mp-header">
        <div className="ai-mp-title">
          {meta ? (
            <PersonaAvatar persona={chat.persona} size={22} className="ai-mp-title-avatar" />
          ) : (
            <Bot size={20} aria-hidden="true" className="ai-mp-title-icon" />
          )}
          <span>
            {meta ? meta.label : 'AI 助手'}
            {chat.sessionMeta?.title ? ` · ${chat.sessionMeta.title}` : ''}
          </span>
          {legacyBanner ? <span className="ai-mp-legacy-badge">只读</span> : null}
        </div>
        <div className="ai-mp-actions">
          <button className="ai-mp-btn" onClick={chat.startNewChat} type="button">
            + 新对话
          </button>
          <button className="ai-mp-btn" onClick={() => setShowSessions(!showSessions)} type="button">
            {showSessions ? '收起' : '会话'}
          </button>
          <button className="ai-mp-btn" onClick={() => setMemoryPanelOpen(true)} type="button">
            个性设置
          </button>
        </div>
      </div>

      {showSessions && (
        <div className="ai-mp-sessions">
          {chat.sessions.length === 0 ? (
            <div className="ai-mp-sessions-empty">暂无会话</div>
          ) : (
            chat.sessions.map((session) => {
              const isLegacy = Boolean(session.legacy) || session.persona == null;
              return (
                <div
                  key={session.session_id}
                  className={`ai-mp-session-item ${chat.sessionId === session.session_id ? 'active' : ''}`}
                  onClick={() => {
                    chat.selectSession(session.session_id);
                    setShowSessions(false);
                  }}
                >
                  <PersonaAvatar persona={session.persona ?? null} size={22} className="ai-mp-session-avatar" />
                  <div className="ai-mp-session-content">
                    <div className="ai-mp-session-text">{sessionListLabel(session.title, session.last_message)}</div>
                    <div className="ai-mp-session-date">
                      {session.created_at?.slice(0, 10)}
                      {isLegacy ? ' · 只读' : ''}
                    </div>
                  </div>
                  <button
                    className="ai-mp-session-delete"
                    onClick={(e) => requestDeleteSession(session.session_id, e)}
                    type="button"
                    aria-label="删除会话"
                  >
                    ×
                  </button>
                </div>
              );
            })
          )}
        </div>
      )}

      {chat.view === 'personaSelect' ? (
        <div className="ai-mp-messages">
          <PersonaPicker
            onPick={(persona) => void chat.createSessionForPersona(persona)}
            disabled={chat.sessionCreating}
            variant="mobile"
          />
        </div>
      ) : (
        <div className="ai-mp-messages" ref={chat.messagesScrollRef} onScroll={chat.onMessagesScroll}>
          {legacyBanner && (
            <div className="ai-mp-legacy-notice">这是一段历史会话，没有角色信息，仅供查看，不能继续对话。</div>
          )}
          {chat.personaUnavailable && (
            <div className="ai-mp-maintenance-notice" role="status">
              {ORGANIZER_MAINTENANCE_MESSAGE}
            </div>
          )}

          {chat.historyLoading && <div className="ai-mp-empty-hint">正在加载历史消息...</div>}

          {!chat.historyLoading && chat.messages.length === 0 && meta && (
            <div className="ai-mp-empty">
              <PersonaAvatar persona={chat.persona} size={40} className="ai-mp-empty-icon" />
              <div className="ai-mp-empty-title">{meta.tagline}</div>
              <div className="ai-mp-empty-hint">{meta.emptyHint}</div>
            </div>
          )}

          {chat.messages.map((msg) => {
            if (msg.kind === 'source_candidates') {
              return (
                <div key={msg.id} className="ai-mp-message assistant">
                  <SourceCandidateCard candidateMsg={msg} onConfirm={chat.confirmCandidates} onCancel={chat.cancelCandidates} />
                </div>
              );
            }

            if (msg.kind === 'stream') {
              const streamMsg = msg as StreamChatMessage;
              return (
                <div key={msg.id} className={`ai-mp-message ${msg.role === 'user' ? 'user' : 'assistant'}`}>
                  <div className="ai-mp-bubble">
                    <div className="ai-mp-text">
                      <StreamMessageText
                        text={streamMsg.content}
                        sessionId={chat.sessionId || undefined}
                        references={streamMsg.references}
                        onCitationClick={(idx) => chat.openCitation(idx, streamMsg.references)}
                        hasReference={(idx) => (streamMsg.references || []).some((r) => r.display_index === idx)}
                      />
                    </div>
                  </div>
                  {msg.role === 'assistant' && streamMsg.references && streamMsg.references.length > 0 && (
                    <StreamReferenceChips
                      sessionId={chat.sessionId || ''}
                      references={streamMsg.references}
                      activeToken={chat.activeReferencePreview?.ref_token ?? null}
                      onSelect={(ref) => chat.openCitation(ref.display_index, streamMsg.references)}
                      variant="mobile"
                    />
                  )}
                </div>
              );
            }

            const legacyMsg = msg as LegacyMessage;
            const legacyIndex = legacyAnalysisMessages.indexOf(legacyMsg);
            return (
              <div key={msg.id} className={`ai-mp-message ${msg.role === 'user' ? 'user' : 'assistant'}`}>
                <div className="ai-mp-bubble">
                  <div className="ai-mp-text">{legacyMsg.content}</div>

                  {msg.role === 'assistant' && legacyMsg.agentSteps && legacyMsg.agentSteps.length > 0 && (
                    <div className="ai-mp-agent-steps">
                      <div className="ai-mp-agent-title">Agent 工具轨迹</div>
                      {legacyMsg.agentSteps.map((step, idx) => (
                        <div key={idx} className="ai-mp-agent-step">
                          <span>{step.title}</span>
                          <small>{step.summary}</small>
                        </div>
                      ))}
                    </div>
                  )}

                  {msg.role === 'assistant' && legacyMsg.analysis && (
                    <>
                      <AnalysisEvidenceBlock analysis={legacyMsg.analysis} variant="mobile" onEntryClick={handleJumpToEntry} />
                      {hasAnalysisDetailContent(legacyMsg.analysis) && (
                        <button
                          type="button"
                          className="ai-mp-analysis-detail-toggle"
                          onClick={() => setAnalysisDetailIndex(legacyIndex)}
                        >
                          查看分析详情
                        </button>
                      )}
                    </>
                  )}

                  {msg.role === 'assistant' && legacyMsg.organizePreview && (
                    <div className="ai-mp-preview-card">
                      <div className="ai-mp-preview-title">整合建议</div>
                      <div className="ai-mp-preview-copy">{legacyMsg.organizePreview.message}</div>
                      <OrganizeDiffPanel
                        preview={legacyMsg.organizePreview}
                        onSaved={chat.handleOrganizeSaved}
                        onPreviewSource={handleOrganizePreviewSource}
                      />
                    </div>
                  )}
                </div>

                {msg.role === 'assistant' && legacyMsg.references && legacyMsg.references.length > 0 && (
                  <ReferenceList
                    references={legacyMsg.references}
                    variant="mobile"
                    activeKey={selectedReferenceKey}
                    onSelect={handleOpenLegacyReference}
                  />
                )}
              </div>
            );
          })}

          {chat.isStreaming && (
            <div className="ai-mp-message assistant">
              <div className="ai-mp-bubble">
                <div className="ai-mp-loading">
                  <Loader2 size={16} aria-hidden="true" className="ai-mp-loading-spin" />
                  <span>{chat.streamingStatus || '处理中...'}</span>
                </div>
              </div>
            </div>
          )}

          {chat.error && (
            <div className="ai-mp-error">
              <span>{chat.error}</span>
              {chat.canRetry && (
                <button type="button" className="growth-organize-btn" onClick={() => void chat.retryLastFailedSend()}>
                  重试
                </button>
              )}
              <button type="button" className="ai-mp-dismiss" onClick={chat.clearError} aria-label="关闭错误提示">×</button>
            </div>
          )}

          {chat.notice && (
            <div className="ai-mp-notice">
              {chat.notice}
              <button type="button" className="ai-mp-dismiss" onClick={chat.clearNotice} aria-label="关闭提示">×</button>
            </div>
          )}

          {pendingActions.length > 0 && (
            <div className="ai-mp-pending-actions">
              <div className="ai-mp-pending-title">待确认动作</div>
              {pendingActions.map((action) => (
                <div key={action.id} className="ai-mp-pending-card">
                  <div className="ai-mp-pending-name">{pendingActionTitle(action)}</div>
                  <div className="ai-mp-pending-copy">{pendingActionSummary(action)}</div>
                  <div className="ai-mp-pending-buttons">
                    <button type="button" onClick={() => handleRejectPendingAction(action.id)} className="ai-mp-pending-btn">
                      拒绝
                    </button>
                    <button type="button" onClick={() => handleConfirmPendingAction(action.id)} className="ai-mp-pending-btn primary">
                      确认
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {chat.view === 'chat' && !legacyBanner && (
        <div className="ai-mp-input-area">
          {chat.persona === 'organizer' && !chat.personaUnavailable && (
            <OrganizeScopeBar
              value={organizeScope}
              onChange={setOrganizeScope}
              compact
              stageLabel={chat.organizeStage}
            />
          )}
          {chat.persona === 'explainer' && chat.sessionId && (
            <LockedSourcesBar
              sources={chat.lockedSources}
              loading={chat.lockedSourcesLoading}
              expanding={chat.expandingSources}
              error={chat.lockedSourcesError}
              disabled={!chat.canSend || chat.isStreaming}
              onExpand={() => chat.openExpandTopicDialog()}
              onPreview={(member) => chat.openReferencePreview(member)}
              variant="mobile"
              expandButtonRef={expandBtnRef}
            />
          )}
          {chat.sessionMeta?.stale && <div className="ai-mp-stale-notice">来源已过期，请重新提问以生成新的来源版本。</div>}
          <div className="ai-mp-input-row">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyPress}
              placeholder={meta?.placeholder || '输入消息……'}
              className="ai-mp-input"
              rows={1}
              disabled={!chat.canSend && !chat.isStreaming}
            />
            {chat.isStreaming ? (
              <button className="ai-mp-send" onClick={chat.stopStreaming} type="button">
                <Square size={14} aria-hidden="true" />
                停止
              </button>
            ) : (
              <button className="ai-mp-send" onClick={handleSend} disabled={!input.trim() || !chat.canSend} type="button">
                <Send size={14} aria-hidden="true" />
                发送
              </button>
            )}
          </div>
        </div>
      )}

      <AnalysisDetailPanel
        analysis={analysisDetailIndex != null ? legacyAnalysisMessages[analysisDetailIndex]?.analysis : null}
        open={analysisDetailIndex != null}
        onClose={() => setAnalysisDetailIndex(null)}
        variant="mobile"
        onEntryClick={handleJumpToEntry}
      />
      <AgentMemoryPanel open={memoryPanelOpen} onClose={() => setMemoryPanelOpen(false)} variant="mobile" />
      <ExpandTopicDialog
        open={chat.expandTopicOpen}
        variant="mobile"
        loading={chat.expandingSources}
        error={chat.expandTopicError}
        initialTopic={chat.expandTopicDraft}
        onClose={chat.closeExpandTopicDialog}
        onSubmit={chat.submitExpandTopic}
        returnFocusRef={expandBtnRef}
      />
      <ConfirmDialog
        open={deleteSessionId != null}
        title="删除 AI 会话"
        message="确认删除这段会话吗？删除后无法恢复。"
        confirmLabel="删除"
        destructive
        loading={deleteSessionLoading}
        error={deleteSessionError}
        onClose={() => {
          if (!deleteSessionLoading) setDeleteSessionId(null);
        }}
        onConfirm={handleDeleteSession}
      />
    </div>
  );
}
